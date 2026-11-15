# -*- coding: utf-8 -*-
"""KiCad → 嘉立创EDA 文件夹工程（.eprj3）—— 路线 A：文档级重建，不需要任何 GUI 权限。

结构以官方转换器 `easyeda/kicad-to-easyeda-eprj3` 的输出为模板（boilerplate 直接复用）：
  <name>.eprj3           工程索引
  pcb/PCB1.epcb2         记录流：FOOTPRINT 段 × 唯一封装 + DEVICE 段 × 唯一封装 + PCB 段
  sch/…                  原理图（本脚本不生成，工程可只含 PCB）

用法：
    python tools/kicad2eprj3.py board.kicad_pcb -o out/eprj3 --name MyBoard
    python tools/kicad2eprj3.py board.kicad_pcb -o out/eprj3 --template <参考.epcb2>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.kicad2epro2 import (MIL, LayerMapper, _chain_edges, _poly_path,  # noqa: E402
                               _rdp, layer_id, mil)

try:                                   # 模块级占位，build() 里再绑定真正的 pcbnew
    import pcbnew  # noqa: F401
except ImportError:                    # pragma: no cover
    pcbnew = None

BOILER_TYPES = {"PRIMITIVE", "LAYER", "LAYER_PHYS", "ACTIVE_LAYER", "RULE_TEMPLATE",
                "RULE", "SILK_OPTS", "D3_ATTRIBUTE", "PREFERENCE", "PANELIZE",
                "RULE_SELECTOR"}


def rec(head: dict, body: dict) -> str:
    return (json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "||"
            + json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "|")


def split_docs(text: str):
    docs, cur = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        if '"DOCHEAD"' in line:
            body = line.split("}||", 1)[1].rstrip("|\n")
            try:
                dt = json.loads(body).get("docType")
            except Exception:
                dt = None
            cur = (dt, [line])
            docs.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return docs


def parse_records(lines):
    out = []
    for line in lines:
        head_s, _, body_s = line.partition("}||")
        try:
            head = json.loads(head_s + "}")
        except Exception:
            continue
        try:
            body = json.loads(body_s.rstrip("|\n")) if body_s.strip() else {}
        except Exception:
            body = {}
        out.append((head, body))
    return out


def doc_stream(doc_type: str, records: list[str], doc_uuid: str | None = None) -> str:
    """生成一个文档段：DOCHEAD + 记录。doc_uuid 必须与引用它的记录一致。"""
    head = {"type": "DOCHEAD"}
    body = {"docType": doc_type, "client": uuid.uuid4().hex[:16],
            "uuid": doc_uuid or uuid.uuid4().hex[:16],
            "updateTime": int(__import__("time").time() * 1000),
            "version": "1", "editVersion": "4.1.36", "user": {}}
    return rec(head, body) + "\n" + "\n".join(records) + "\n"


class Doc:
    """一条记录流的小工具：自动分配 ticket。"""

    def __init__(self):
        self.ticket = 0
        self.lines: list[str] = []

    def add(self, typ: str, rid, body: dict) -> None:
        self.ticket += 1
        head = {"type": typ, "ticket": self.ticket}
        head["id"] = (json.dumps(list(rid), ensure_ascii=False, separators=(",", ":"))
                      if isinstance(rid, (list, tuple)) else rid)
        self.lines.append(rec(head, body))


def _exp_mil(pad, layer) -> float | None:
    """焊盘在该层的阻焊/钢网扩展（mil）。None 表示遵循规则。

    KiCad 10 起 `GetSolderPasteMargin()` / `GetSolderMaskExpansion()` 返回 **VECTOR2I**（X/Y 两轴），
    旧代码把它当标量做除法，异常被吞掉后静默变成 None（等于回落到规则默认值）。
    这里两轴相同就给单值，不同则分别返回并打印一次告警（EDA 的扩展是单值）。
    """
    try:
        if layer in (pcbnew.F_Mask, pcbnew.B_Mask):
            val = pad.GetSolderMaskExpansion(layer)
        else:
            val = pad.GetSolderPasteMargin(layer)
    except Exception:
        return None
    try:
        if hasattr(val, "x") and hasattr(val, "y"):
            x, y = mil(val.x / 1e6), mil(val.y / 1e6)
            if abs(x - y) > 0.01:
                global _exp_asym_warned
                if not _exp_asym_warned:
                    _exp_asym_warned = True
                    print(f"  ! 焊盘 {pad.GetNumber()} 在 {pcbnew.GetBoard().GetLayerName(layer) if False else layer} "
                          f"的扩展 X={x}mil / Y={y}mil 不同；EDA 只支持单值，取较大者")
                return round(max(x, y), 3)
            return round(x, 3)
        return round(mil(val / 1e6), 3)
    except Exception:
        return None


_exp_asym_warned = False


def fp_signature(fp) -> str:
    """封装实例的内容指纹：焊盘几何/孔/偏孔/角度/层/连接方式 + 图元 + 内嵌 zone。"""
    src = []
    base_ang = fp.GetOrientationDegrees()
    src = [("SIDE", "B" if fp.GetLayer() == pcbnew.B_Cu else "F")]
    for _p in fp.Pads():
        _sz, _dr, _of = _p.GetSize(), _p.GetDrillSize(), _p.GetOffset()
        try:
            _rel = _p.GetFPRelativePosition()
            _pos = (_rel.x, _rel.y)
        except Exception:
            _pos = (0, 0)
        # 阻焊/钢网扩展、圆角比例都参与指纹：否则"只有扩展不同"的两个实例会被合并成一个封装
        _exp = tuple(_exp_mil(_p, _lay) for _lay in
                     (pcbnew.F_Mask, pcbnew.B_Mask, pcbnew.F_Paste, pcbnew.B_Paste))
        try:
            _rr = round(_p.GetRoundRectRadiusRatio(), 4)
        except Exception:
            _rr = None
        src.append(("PAD", _p.GetNumber(), int(_p.GetShape()), _sz.x, _sz.y,
                    _dr.x, _dr.y, _of.x, _of.y, _pos,
                    round((_p.GetOrientationDegrees() - base_ang) % 360, 1),
                    tuple(sorted(str(li) for li in _p.GetLayerSet().Seq())),
                    _p.GetLocalZoneConnection(), _p.GetAttribute(), _exp, _rr))
    # 图元用"未旋转的封装局部坐标"参与指纹，否则同一封装换位置/角度就会被判成不同变体
    _pos = fp.GetPosition()
    _th = math.radians(fp.GetOrientationDegrees())
    _c, _sn = math.cos(_th), math.sin(_th)

    def _loc(v):
        # 量化到 0.00254mm（0.1mil），抵消浮点噪声，保证"同形不同摆放"指纹一致
        _dx, _dy = v.x - _pos.x, v.y - _pos.y
        return (int(round((_dx * _c + _dy * _sn) / 2540.0)),
                int(round((-_dx * _sn + _dy * _c) / 2540.0)))

    for _it in fp.GraphicalItems():
        try:
            # 线段方向不参与指纹（旋转后起终点会互换）
            try:
                _w = _it.GetWidth()
            except Exception:
                _w = 0
            src.append(("G", type(_it).__name__, _it.GetLayer(), int(_it.GetShape()), _w,
                        tuple(sorted([_loc(_it.GetStart()), _loc(_it.GetEnd())]))))
        except Exception:
            continue
    for _z in fp.Zones():
        try:
            src.append(("Z", str(tuple(_z.GetLayerSet().Seq())),
                        _z.Outline().Outline(0).PointCount()))
        except Exception:
            continue
    return hashlib.md5(repr(sorted(map(str, src))).encode()).hexdigest()[:8]


def arc_points(sh, max_step_deg: float = 0.5, chord_mm: float = 0.05):
    """按 起点/中点/终点 恢复真实弧段（方向由中点决定），返回点列。

    注意两个类的方法名不同：`PCB_ARC`（曲线走线）只有 `GetMid()`，
    `PCB_SHAPE`（板框图元）只有 `GetArcMid()`。早期只认后者的写法在
    KiCad 9 的曲线走线上会抛 AttributeError，退化成"方向靠猜"，把弧画到另一侧。
    """
    import math as _m

    c, s, e = sh.GetCenter(), sh.GetStart(), sh.GetEnd()
    r = _m.hypot(s.x - c.x, s.y - c.y)
    if r <= 0:
        return [s, e]
    a0 = _m.atan2(s.y - c.y, s.x - c.x)
    a1 = _m.atan2(e.y - c.y, e.x - c.x)

    mid = None
    for name in ("GetMid", "GetArcMid"):          # PCB_ARC / PCB_SHAPE
        fn = getattr(sh, name, None)
        if not callable(fn):
            continue
        try:
            candidate = fn()
        except Exception:
            candidate = None
        if candidate is not None and (candidate.x or candidate.y):
            mid = candidate
            break

    if mid is not None:
        am = _m.atan2(mid.y - c.y, mid.x - c.x)
        ccw_sweep = (a1 - a0) % (2 * _m.pi)
        ccw_mid = (am - a0) % (2 * _m.pi)
        sweep = ccw_sweep if ccw_mid <= ccw_sweep else -(2 * _m.pi - ccw_sweep)
    else:
        # 兜底：默认逆时针；只有明确是顺时针才翻转
        sweep = (a1 - a0) % (2 * _m.pi)
        try:
            if bool(sh.IsClockwiseArc()):
                sweep = -(2 * _m.pi - sweep)
        except Exception:
            pass

    span_deg = abs(_m.degrees(sweep))
    n = max(2, int(span_deg / max_step_deg) + 1)
    # 半径大的弧按"弦高不超过 chord_mm"再细分，避免几十段折线在 3D 里看出棱角
    r_mm = r / 1e6
    if r_mm > chord_mm:
        step_by_chord = 2.0 * _m.degrees(_m.acos(max(-1.0, 1.0 - chord_mm / r_mm)))
        if step_by_chord > 0:
            n = max(n, int(span_deg / step_by_chord) + 1)
    n = min(n, 512)
    pts = []
    for i in range(n + 1):
        th = a0 + sweep * i / n
        pts.append(pcbnew.VECTOR2I(int(round(c.x + r * _m.cos(th))),
                                   int(round(c.y + r * _m.sin(th)))))
    pts[0], pts[-1] = s, e
    return pts


def circle_points(sh, max_step_deg: float = 0.5):
    """整圆离散（SHAPE_T_CIRCLE）。

    注意：KiCad 的 PCB_SHAPE 圆把 **GetStart() 存成圆心**、`GetEnd()` 才是圆周上一点，
    所以老代码用 `|start - center|` 算半径恒为 0，圆被**静默丢弃**
    （测试点的外圈丝印整圈消失就是这么来的）。这里按 GetRadius → GetEnd → 包围盒 依次兜底。
    """
    import math as _m

    try:
        c = sh.GetCenter()
    except Exception:
        c = sh.GetStart()
    cx, cy = c.x, c.y

    r = 0.0
    for getter in ("GetRadius", "GetShapeSize"):
        if r > 0:
            break
        try:
            val = getattr(sh, getter)()
            r = float(getattr(val, "x", val))
        except Exception:
            continue
    if r <= 0:
        try:
            e = sh.GetEnd()
            r = _m.hypot(e.x - cx, e.y - cy)
        except Exception:
            r = 0.0
    if r <= 0:
        try:
            bb = sh.GetBoundingBox()
            r = (bb.GetWidth() - sh.GetWidth()) / 2.0
        except Exception:
            r = 0.0
    if r <= 0:
        return []
    n = min(360, max(16, int(360.0 / max_step_deg)))   # 1° 步进，兼顾平滑与体积
    return [pcbnew.VECTOR2I(int(round(cx + r * _m.cos(2 * _m.pi * i / n))),
                            int(round(cy + r * _m.sin(2 * _m.pi * i / n))))
            for i in range(n + 1)]


def _max_deviation(orig, simplified) -> float:
    """原折线各点到简化折线的最大距离（mil，用于降级报告）。"""
    def dist_pt_seg(p, a, b):
        ax, ay = a
        bx, by = b
        px, py = p
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    worst = 0.0
    for p in orig:
        best = min((dist_pt_seg(p, simplified[i], simplified[i + 1])
                    for i in range(len(simplified) - 1)), default=0.0)
        worst = max(worst, best)
    return worst


def circle_geometry(sh):
    """返回 (圆心x_mm, 圆心y_mm, 半径_mm)；取不到返回 None。

    KiCad 的 PCB_SHAPE 圆把 GetStart() 存成圆心、GetEnd() 是圆周上一点，
    所以半径不能用 |start - center| 计算（那是 0，会把圆整圈丢掉）。
    """
    import math as _m
    try:
        c = sh.GetCenter()
    except Exception:
        c = sh.GetStart()
    r = 0.0
    try:
        r = float(sh.GetRadius())
    except Exception:
        try:
            e = sh.GetEnd()
            r = _m.hypot(e.x - c.x, e.y - c.y)
        except Exception:
            r = 0.0
    if r <= 0:
        return None
    return c.x / 1e6, c.y / 1e6, r / 1e6


def _pad_window_polygon(pad) -> list[tuple[float, float]]:
    """焊盘在板级坐标下的外轮廓（mil，y 取负与 EDA 一致）。

    用 KiCad 的 `GetEffectivePolygon()`：圆/椭圆/长圆/圆角矩形/自定义形状都由它给出，
    避免一律退化成外接矩形。
    """
    # 圆/椭圆：自己生成 64 边形（KiCad 的 GetEffectivePolygon 只给 ~25 边形，
    # 圆窗面积会偏小 1.1%）；其余形状交给 KiCad 的真实轮廓。
    try:
        _sh = pad.GetShape()
        _sz = pad.GetSize()
        _ap = pad.GetPosition()
        _ang = math.radians(pad.GetOrientationDegrees())
        _ca, _sa = math.cos(_ang), math.sin(_ang)
        if _sh == pcbnew.PAD_SHAPE_CIRCLE and _sz.x == _sz.y:
            _r = _sz.x / 2.0
            _n = 64
            _pts = []
            for _i in range(_n + 1):
                _a = 2 * math.pi * _i / _n
                _lx, _ly = _r * math.cos(_a), _r * math.sin(_a)
                _mx = _ap.x + (_lx * _ca - _ly * _sa)
                _my = _ap.y + (_lx * _sa + _ly * _ca)
                _pts.append((round(mil(_mx / 1e6), 4), -round(mil(_my / 1e6), 4)))
            return _pts
    except Exception:
        pass
    # 其余形状用 ERROR_OUTSIDE：多边形包住真实轮廓（宁可略大，避免少锡）
    try:
        poly = pad.GetEffectivePolygon(pcbnew.ERROR_OUTSIDE)
    except Exception:
        try:
            poly = pad.GetEffectivePolygon(0)
        except Exception:
            poly = None
    if poly is None or poly.OutlineCount() == 0:
        # 兜底：退回外接矩形（并在控制台提示）
        sz = pad.GetSize()
        w, h = mil(sz.x / 1e6), mil(sz.y / 1e6)
        ang = math.radians(pad.GetOrientationDegrees())
        ca, sa = math.cos(ang), math.sin(ang)
        ap = pad.GetPosition()
        hw, hh = w / 2.0, h / 2.0
        out = []
        for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
            mx = ap.x + (dx * ca - dy * sa) * 25400.0
            my = ap.y + (dx * sa + dy * ca) * 25400.0
            out.append((mil(mx / 1e6), -mil(my / 1e6)))
        print(f"  ! 焊盘 {pad.GetNumber()} 取不到轮廓，钢网窗口退回矩形")
        return out
    ch = poly.Outline(0)
    return [(round(mil(ch.CPoint(i).x / 1e6), 4), -round(mil(ch.CPoint(i).y / 1e6), 4))
            for i in range(ch.PointCount())]


def simplify_polyline(pts, limit: int | None = None, closed: bool | None = None):
    """按误差受控简化折线。返回 (点列, info|None)。

    设计约束（对应评审 N02）：
      * **不做等距预抽点**——那会把尖峰一起抽掉；
      * **不做 `[:limit]` 截断**——那会丢尾部与闭合性；
      * 始终保留首点、末点；闭合图形额外保证末点 == 首点；
      * 在 ε 逐级放大的过程中找"点数 ≤ limit 且最大偏差 ≤ ε"的结果；
      * 无论如何达不到时，返回最接近的结果并标记降级，**不牺牲几何**。
    """
    lim = SILK_POLY_LIMIT if limit is None else limit
    if lim <= 0 or len(pts) <= lim:
        return pts, None
    if closed is None:
        closed = len(pts) > 3 and tuple(pts[0]) == tuple(pts[-1])

    eps_max_mil = SILK_SIMPLIFY_EPS_MM * MIL
    eps = 0.02 * MIL
    best = None
    best_eps = 0.0
    while eps <= eps_max_mil * 4:              # 允许超一点，用于判定"能不能满足"
        cand = _rdp(list(pts), eps)
        if closed and cand and tuple(cand[0]) != tuple(cand[-1]):
            cand = cand + [cand[0]]
        if best is None or len(cand) < len(best):
            best, best_eps = cand, eps
        if len(cand) <= lim:
            dev = _max_deviation(pts, cand)
            return cand, {"src_points": len(pts), "out_points": len(cand),
                          "eps_mm": round(eps / MIL, 4),
                          "max_dev_mm": round(dev / MIL, 4), "degraded": False}
        eps *= 1.6

    dev = _max_deviation(pts, best)
    return best, {"src_points": len(pts), "out_points": len(best),
                  "eps_mm": round(best_eps / MIL, 4),
                  "max_dev_mm": round(dev / MIL, 4), "degraded": True,
                  "reason": f"在 {SILK_SIMPLIFY_EPS_MM} mm 内无法压到 {lim} 点"}


def shape_polylines(sh, max_step_deg: float = 0.5):
    """把任意 SHAPE 拆成若干折线（矩形按四角闭合，弧/圆按真实方向离散，直线原样）。

    早期 RECT/BEZIER 落到最后一行，只输出 start→end 两个点，
    等于给矩形画了一条**对角线**（C3 顶层丝印那 9 条莫名其妙的斜线就是这么来的）。
    """
    st = sh.GetShape()
    if st == pcbnew.SHAPE_T_ARC:
        return [arc_points(sh, max_step_deg)]
    if st == pcbnew.SHAPE_T_CIRCLE:
        return [circle_points(sh, max_step_deg)]
    if st == pcbnew.SHAPE_T_RECT:
        try:
            corners = list(sh.GetRectCorners())
            if len(corners) >= 4:
                return [corners + [corners[0]]]
        except Exception:
            pass
        s0, e0 = sh.GetStart(), sh.GetEnd()
        pts = [pcbnew.VECTOR2I(s0.x, s0.y), pcbnew.VECTOR2I(e0.x, s0.y),
               pcbnew.VECTOR2I(e0.x, e0.y), pcbnew.VECTOR2I(s0.x, e0.y),
               pcbnew.VECTOR2I(s0.x, s0.y)]
        return [pts]
    if st == pcbnew.SHAPE_T_BEZIER:
        try:                                     # 贝塞尔：交给 KiCad 采样成折线
            poly = sh.GetBezierAsPolyline(0.01) if hasattr(sh, "GetBezierAsPolyline") else None
            if poly:
                return [list(poly)]
        except Exception:
            pass
    if st == pcbnew.SHAPE_T_POLY:
        poly = sh.GetPolyShape()
        out = []
        for oi in range(poly.OutlineCount()):
            ch = poly.Outline(oi)
            out.append([ch.CPoint(i) for i in range(ch.PointCount())])
        return out
    return [[sh.GetStart(), sh.GetEnd()]]


def eda_font(txt, default: str = "Arial") -> str:
    """KiCad 字体名 → EDA 字体名。

    KiCad 的默认是**笔画字体**（`GetFontName()` 返回空串）。EDA 的 `default` 字体
    **不支持"反相"**（实测写 reverse=true 也不渲染成挖空字），换成 Arial 才有效果。
    """
    name = ""
    try:
        name = txt.GetFontName() or ""
    except Exception:
        name = ""
    if not name or name.strip().lower() in ("kicad font", "kicadfont"):
        return default
    return name


def stroke_band(points, width_nm):
    """把折线按线宽扩成闭合带（沿法线双侧偏移）。用于圆弧/圆这类曲线描边。

    EDA 的折线渲染在 3D 里会露出接缝、看着像断续的弧；KiCad 是把它画成描边多边形。
    这里直接生成带多边形，交给 FILL(SOLID) 渲染，观感与 KiCad 一致。
    """
    import math as _m

    pts = [(float(p.x), float(p.y)) for p in points]
    if len(pts) < 2 or width_nm <= 0:
        return []
    half = width_nm / 2.0
    left, right = [], []
    for i, (x, y) in enumerate(pts):
        if i == 0:
            dx, dy = pts[1][0] - x, pts[1][1] - y
        elif i == len(pts) - 1:
            dx, dy = x - pts[i - 1][0], y - pts[i - 1][1]
        else:
            await1 = (x - pts[i - 1][0], y - pts[i - 1][1])
            next2 = (pts[i + 1][0] - x, pts[i + 1][1] - y)
            def _unit(d):
                L = _m.hypot(*d) or 1.0
                return (d[0] / L, d[1] / L)
            a, b = _unit(await1), _unit(next2)
            dx, dy = a[0] + b[0], a[1] + b[1]
        seg = _m.hypot(dx, dy) or 1.0
        ux, uy = dx / seg, dy / seg
        nx, ny = -uy, ux
        # 拐角处按 1/cos(θ/2) 放大，避免内侧过冲
        left.append(pcbnew.VECTOR2I(int(round(x + nx * half)), int(round(y + ny * half))))
        right.append(pcbnew.VECTOR2I(int(round(x - nx * half)), int(round(y - ny * half))))
    return left + right[::-1]


def _item_width_mil(item, default: float = 10.0) -> float:
    w = getattr(item, "GetWidth", default)
    w = w() if callable(w) else w
    return mil(w / 1e6) if w else default


CIRCLE_SEGMENTS = int(os.environ.get("JLC_CIRCLE_SEGMENTS", "72"))
# EDA 实测（experiment：同一封装内 80…2000 点的多边形全部正常渲染并导出）：
# **没有找到"点数上限"**。早期"180 点被丢弃"的结论已被推翻。
# 因此默认不做简化（0 = 关闭）；只在超过该阈值时才做误差受控简化，且**绝不截断尾部**。
SILK_POLY_LIMIT = int(os.environ.get("JLC_SILK_POLY_LIMIT", "2000"))
SILK_SIMPLIFY_EPS_MM = float(os.environ.get("JLC_SILK_EPS_MM", "0.10"))   # 圆丝印离散段数（EDA 对封装内长折线有上限，实测 >24 可用、180 会被丢）
ONLY_REF: str | None = None
SKIP_POURED = True
ENLARGE_VIA = False
VALUE_ATTR = False


def build(board_path: str, out_dir: str, name: str, template: str | None) -> str:
    global pcbnew
    import pcbnew as _pcbnew
    pcbnew = _pcbnew

    if not os.path.isfile(board_path):
        sys.exit(f"板文件不存在：{board_path}")
    board = pcbnew.LoadBoard(board_path)
    if board is None:
        sys.exit(f"无法解析板文件（可能不是 .kicad_pcb 或版本过新）：{board_path}")
    only_ref = getattr(build, "only_ref", None)
    lm = LayerMapper(board, allow_unknown=getattr(build, "allow_unknown_layer", False))

    # ---------- 模板 boilerplate ----------
    boiler = []
    if template and os.path.isfile(template):
        docs = split_docs(open(template, encoding="utf-8").read())
        pcb_lines = next((lines for dt, lines in docs if dt == "PCB"), None)
        if pcb_lines:
            for head, body in parse_records(pcb_lines):
                if head.get("type") in BOILER_TYPES:
                    # 重新编号 ticket，其余原样
                    head["ticket"] = 0
                    boiler.append((head, body))
    if not boiler:
        sys.exit("缺少模板：请用 --template 指向转换器产出的 .epcb2")

    def emit_boiler(doc: Doc, skip=()) -> None:
        for head, body in boiler:
            if head.get("type") in skip:
                continue
            doc.ticket += 1
            h = dict(head)
            h["ticket"] = doc.ticket
            doc.lines.append(rec(h, body))

    # ---------- 遍历 KiCad ----------
    copper = int(board.GetCopperLayerCount())
    used = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12}
    for i in range(1, copper - 1):
        used.add(14 + i)

    nets = [""]
    for _c, net in board.GetNetInfo().NetsByNetcode().items():
        n = net.GetNetname()
        if n and n not in nets:
            nets.append(n)

    # 铺铜热焊参数：按"区域×层"分别取，焊盘按所在层匹配（不再只看第一块 zone）
    _zones = [z for z in board.Zones() if not getattr(z, "GetIsRuleArea", lambda: False)()]
    zone_by_layer = {}                     # layerId(KiCad) → dict(gap, spoke, conn)
    for _z in _zones:
        if _z.GetIsRuleArea():
            continue
        _attr = {"gap": _z.GetThermalReliefGap() / 1e6,
                 "spoke": _z.GetThermalReliefSpokeWidth() / 1e6,
                 "conn": _z.GetPadConnection()}
        for _li in _z.GetLayerSet().Seq():
            zone_by_layer.setdefault(_li, _attr)
    thermal_gap_mm, spoke_mm, zone_conn = 0.25, 0.3, 3
    if zone_by_layer:
        _first = next(iter(zone_by_layer.values()))
        thermal_gap_mm, spoke_mm, zone_conn = _first["gap"], _first["spoke"], _first["conn"]

    def _map_conn(value: int, is_tht: bool) -> str:
        """KiCad ZONE_CONNECTION → EDA EPadConnect（0=NONE 1=THERMAL 2=FULL 3=THT_THERMAL）"""
        if value == 3:
            return "DIVERGENCE" if is_tht else "DIRECT"
        if value == 1:
            return "DIVERGENCE"
        if value == 2:
            return "DIRECT"
        if value == 0:
            return "NON_CONNECT"
        return "DIVERGENCE" if is_tht else "DIRECT"

    def zone_attr_for(pad):
        """返回该焊盘所在层的 zone 热焊属性（没有则用全局默认）。"""
        try:
            for li in pad.GetLayerSet().Seq():
                if li in zone_by_layer:
                    return zone_by_layer[li]
        except Exception:
            pass
        return {"gap": thermal_gap_mm, "spoke": spoke_mm, "conn": zone_conn}

    def pad_connect(is_tht: bool, pad=None) -> str:
        """焊盘自身的 zone_connect 优先，否则继承 zone 的设置。"""
        local = -1
        if pad is not None:
            # 非金属化孔（NPTH）没有铜环，谈不上铺铜连接
            try:
                if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                    return "NON_CONNECT"
            except Exception:
                pass
            try:
                local = pad.GetLocalZoneConnection()
            except Exception:
                local = -1
        if local is not None and local >= 0:
            return _map_conn(local, is_tht)
        _zc = zone_attr_for(pad)["conn"] if pad is not None else zone_conn
        return _map_conn(_zc, is_tht)

    # 唯一封装（按 FPID + 内容指纹去重）
    fps = {}
    for fp in board.GetFootprints():
        if ONLY_REF and fp.GetReference() != ONLY_REF:
            continue
        fpid = fp.GetFPIDAsString()
        fps.setdefault((fpid, fp_signature(fp)), fp)

    _name_count = {}
    for _k in fps:
        _name_count[_k[0]] = _name_count.get(_k[0], 0) + 1
    fp_title = {_k: (_k[0] if _name_count[_k[0]] == 1 else f"{_k[0]}#{_k[1]}")
                for _k in fps}
    fp_uuid = {fpid: uuid.uuid4().hex[:16] for fpid in fps}
    dev_uuid = {fpid: uuid.uuid4().hex[:16] for fpid in fps}

    segments: list[str] = []

    pad_ids: dict[str, list[str]] = {}
    # 物理焊盘序号 → 封装内 padId（PAD_NET 用它区分同号焊盘，见 N01）
    pad_pid_by_index: dict[str, dict[int, str]] = {}
    paste_windows: list[tuple[int, list[tuple[float, float]]]] = []
    degraded: list[dict] = []        # 简化降级清单（写进 degraded_polylines.json）
    # 说明（实测结论）：
    #   * 封装文档里的 POLY **点数过多会被 EDA 丢弃**（721/180 点整圈消失，24/72 点正常）；
    #   * 板级 POLY 能导出到 Gerber，但不进 3D 预览；
    #   * ARC 图元**不支持丝印层**（只能在铜层创建）。
    # 所以圆丝印写进封装文档，段数由 CIRCLE_SEGMENTS 控制。

    # ---------- FOOTPRINT 段 ----------
    for _fkey, _fpobj in fps.items():
        fp = _fpobj
        fpid = _fkey
        fp_name = fp_title[_fkey]
        d = Doc()
        d.add("META", "META", {"title": fp_name, "description": "", "tags": [], "source": ""})
        d.add("CANVAS", "CANVAS", {"originX": 0, "originY": 0})
        fp_uuid_val = fp_uuid[fpid]
        bbox = fp.GetBoundingBox(False, False)
        ox, oy = fp.GetPosition().x, fp.GetPosition().y
        d.add("PART", "pid" + uuid.uuid4().hex[:16],
              {"BBOX": [mil((bbox.GetX() - ox) / 1e6), -mil((bbox.GetBottom() - oy) / 1e6),
                        mil((bbox.GetRight() - ox) / 1e6), -mil((bbox.GetY() - oy) / 1e6)],
               "title": ""})
        origin = fp.GetPosition()

        theta = math.radians(fp.GetOrientationDegrees())
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        # 底面器件：编辑器会把底面器件整体镜像，这里先镜像 X 一次抵消
        is_bottom = fp.GetLayer() == pcbnew.B_Cu

        def rel(pt):
            """绝对坐标 → 封装未旋转局部坐标（KiCad 角度按逆时针，屏幕 Y 向下）。"""
            dx = (pt.x - origin.x) / 1e6
            dy = (pt.y - origin.y) / 1e6
            lx = dx * cos_t - dy * sin_t
            ly = dx * sin_t + dy * cos_t
            if is_bottom:
                ly = -ly
            return (mil(lx), -mil(ly))

        # 铜箔图形的网络：按"覆盖到的焊盘"推断，优先信号网络（天线焊盘常是 信号 + GND 两个）
        _pad_net_list = [(str(p.GetNumber()), p.GetNetname()) for p in fp.Pads() if p.GetNetname()]
        _unique_nets = sorted({n for _num, n in _pad_net_list})
        fp_net = _unique_nets[0] if len(_unique_nets) == 1 else ""

        _GROUND_LIKE = {"GND", "GNDA", "GNDD", "AGND", "DGND", "VSS", "0", "EARTH",
                        "VCC", "VDD", "+3V3", "3V3", "VBUS", "VIN", "+5V", "5V"}

        def copper_net_for(poly_pts_abs):
            """多边形覆盖到的焊盘里挑一个网络：优先非电源/地网络，其次按焊盘编号。"""
            if fp_net:
                return fp_net
            xs = [x for x, _y in poly_pts_abs]
            ys = [y for _x, y in poly_pts_abs]
            if not xs:
                return ""
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            inside = []
            for pad in fp.Pads():
                net = pad.GetNetname()
                if not net:
                    continue
                pp = pad.GetPosition()
                px, py = pp.x / 1e6, pp.y / 1e6
                if x0 - 0.2 <= px <= x1 + 0.2 and y0 - 0.2 <= py <= y1 + 0.2:
                    inside.append((str(pad.GetNumber()), net))
            if not inside:
                return ""
            inside.sort()
            for _num, net in inside:
                if net.upper() not in _GROUND_LIKE:
                    return net
            return inside[0][1]

        idx = 0
        for item in fp.GraphicalItems():
            layer = item.GetLayer()
            is_copper_graphic = pcbnew.IsCopperLayer(layer)
            if layer not in (pcbnew.F_SilkS, pcbnew.B_SilkS) and not is_copper_graphic:
                continue
            silk_layer = lm.get(layer) if is_copper_graphic else \
                (3 if layer == pcbnew.F_SilkS else 4)

            # ① 封装内丝印文字 → STRING（局部坐标）
            if isinstance(item, pcbnew.PCB_TEXT):
                if is_copper_graphic:
                    continue
                txt = item.GetText() or ""
                if not txt.strip():
                    continue
                hgt = item.GetTextHeight() / 1e6
                thk = item.GetTextThickness() / 1e6
                lx, ly = rel(item.GetPosition())
                d.add("STRING", uuid.uuid4().hex[:16],
                      {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                       "layerId": silk_layer, "x": lx, "y": ly, "text": txt,
                       "fontFamily": eda_font(item), "fontSize": mil(hgt),
                       "strokeWidth": mil(thk if thk > 0 else hgt / 8.0),
                       "bold": 0, "italic": 0, "origin": "CENTER_MIDDLE",
                       "angle": round(item.GetTextAngleDegrees() % 360.0, 3),
                       "reverse": False, "expansion": 0,
                       "mirror": layer == pcbnew.B_SilkS, "specialColor": None})
                continue

            # ② 多边形（实心三角、1 脚标记、**铜层天线**等）
            if isinstance(item, pcbnew.PCB_SHAPE) and item.GetShape() == pcbnew.SHAPE_T_POLY:
                poly = item.GetPolyShape()
                if poly.OutlineCount() == 0:
                    continue
                ch = poly.Outline(0)
                pts = [rel(ch.CPoint(i)) for i in range(ch.PointCount())]
                if len(pts) < 3:
                    continue
                idx += 1
                if is_copper_graphic:
                    # 铜层闭合多边形 = 一块铜箔，用 FILL(SOLID) 表达（POLY 只是折线描边）
                    # 封装内 POLY 的轮廓本来就是**绝对板坐标**，不要再叠加器件原点
                    _abs_pts = [(ch.CPoint(i).x / 1e6, ch.CPoint(i).y / 1e6)
                                for i in range(ch.PointCount())]
                    d.add("FILL", uuid.uuid4().hex[:16],
                          {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                           "netName": copper_net_for(_abs_pts), "layerId": silk_layer, "width": 0,
                           "fillStyle": "SOLID", "path": [_poly_path(pts, closed=True)]})
                else:
                    _pts2, _info = simplify_polyline(pts, closed=True)
                    if _info and _info.get("degraded"):
                        degraded.append({"doc": fp_name, "where": "footprint-poly",
                                         "layer": silk_layer, **_info})
                    d.add("POLY", f"{fp_uuid_val}p{idx}",
                          {"groupId": 0, "netName": "", "layerId": silk_layer,
                           "width": _item_width_mil(item),
                           "path": _poly_path(_pts2, closed=True),
                           "locked": False, "zIndex": 4, "polyType": "NORMAL"})
                continue

            # ③ 直线 / 圆弧 / 圆（按真实走向离散）
            if not hasattr(item, "GetStart"):
                continue
            if (isinstance(item, pcbnew.PCB_SHAPE)
                    and item.GetShape() == pcbnew.SHAPE_T_CIRCLE):
                # 圆：写进封装文档（EDA 板级 POLY 不进 3D），段数受 CIRCLE_SEGMENTS 限制
                _geo = circle_geometry(item)
                if _geo:
                    import math as _m
                    _cx, _cy, _r = _geo
                    _pts = [rel(pcbnew.VECTOR2I(
                        int(round(_cx * 1e6 + _r * 1e6 * _m.cos(2 * _m.pi * i / CIRCLE_SEGMENTS))),
                        int(round(_cy * 1e6 + _r * 1e6 * _m.sin(2 * _m.pi * i / CIRCLE_SEGMENTS)))))
                        for i in range(CIRCLE_SEGMENTS + 1)]
                    idx += 1
                    d.add("POLY", f"{fp_uuid_val}p{idx}",
                          {"groupId": 0, "netName": fp_net if is_copper_graphic else "",
                           "layerId": silk_layer, "width": _item_width_mil(item),
                           "path": _poly_path(_pts), "locked": False, "zIndex": 4,
                           "polyType": "NORMAL"})
                continue
            for pl in shape_polylines(item, max_step_deg=5.0):
                pts = [rel(pt) for pt in pl]
                if len(pts) < 2:
                    continue
                idx += 1
                _pts3, _info3 = (pts, None) if is_copper_graphic else simplify_polyline(pts)
                if _info3 and _info3.get("degraded"):
                    degraded.append({"doc": fp_name, "where": "footprint-shape",
                                     "layer": silk_layer, **_info3})
                d.add("POLY", f"{fp_uuid_val}p{idx}",
                      {"groupId": 0, "netName": fp_net if is_copper_graphic else "",
                       "layerId": silk_layer,
                       "width": _item_width_mil(item),
                       "path": _poly_path(_pts3),
                       "locked": False, "zIndex": 4, "polyType": "NORMAL"})

        # 封装内 keepout（如 ESP32 模块的天线禁区）→ 封装文档里的 REGION(PROHIBIT)
        try:
            fp_zones = list(fp.Zones())
        except Exception:
            fp_zones = []
        for fz in fp_zones:
            if not getattr(fz, "GetIsRuleArea", lambda: False)():
                continue
            prohibits = []
            if (getattr(fz, "GetDoNotAllowZoneFills", lambda: False)()
                    or getattr(fz, "GetDoNotAllowCopperPour", lambda: False)()):
                prohibits.append("COPPER")
            if getattr(fz, "GetDoNotAllowTracks", lambda: False)():
                prohibits.append("TRACK")
            if getattr(fz, "GetDoNotAllowVias", lambda: False)():
                prohibits.append("VIA")
            if (getattr(fz, "GetDoNotAllowPads", lambda: False)()
                    or getattr(fz, "GetDoNotAllowFootprints", lambda: False)()):
                prohibits.append("COMPONENT")
            if not prohibits:
                prohibits = ["COPPER", "TRACK", "VIA"]
            fo = fz.Outline()
            if fo.OutlineCount() == 0:
                continue
            fchain = fo.Outline(0)
            # 实测：fp.Zones() 的轮廓是**绝对板坐标**（移动封装会跟着变），
            # 写进封装文档必须换成"未旋转局部坐标"，否则会被器件位置二次偏移。
            zpts = [rel(fchain.CPoint(i)) for i in range(fchain.PointCount())]
            if len(zpts) < 3:
                continue
            for lid_k in fz.GetLayerSet().Seq():
                if not pcbnew.IsCopperLayer(lid_k):
                    continue
                d.add("REGION", uuid.uuid4().hex[:16],
                      {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                       "width": mil(0.254), "prohibitType": prohibits,
                       "path": [_poly_path(zpts, closed=True)], "name": "",
                       "regionType": "PROHIBIT",
                       "layerId": lm.get(lid_k)})

        pad_ids[fpid] = []
        pad_pid_by_index[fpid] = {}
        for _pad_index, pad in enumerate(fp.Pads()):
            size, drill = pad.GetSize(), pad.GetDrillSize()
            shape = pad.GetShape()
            # ---- 孔与槽方向（先算，供焊盘形状与角度使用）----
            hole = None
            slot_rel_angle = 0
            slot_extra_angle = 0
            if drill.x > 0 and drill.y > 0:
                is_slot = abs(drill.x - drill.y) > 1000          # nm，>1µm 视为槽
                if not is_slot:
                    hole = {"holeType": "ROUND", "width": mil(drill.x / 1e6),
                            "height": mil(drill.x / 1e6)}
                else:
                    # 编辑器只接受 width=长边/height=短边的槽；纵槽靠"焊盘整体转 90°"表达
                    if drill.x >= drill.y:
                        hole = {"holeType": "SLOT", "width": mil(drill.x / 1e6),
                                "height": mil(drill.y / 1e6)}
                    else:
                        hole = {"holeType": "SLOT", "width": mil(drill.y / 1e6),
                                "height": mil(drill.x / 1e6)}
                        slot_extra_angle = 90
            w, h = mil(size.x / 1e6), mil(size.y / 1e6)
            if slot_extra_angle:
                w, h = h, w
            if shape == pcbnew.PAD_SHAPE_CIRCLE:
                default_pad = {"padType": "ELLIPSE", "width": w, "height": h}
            elif shape == pcbnew.PAD_SHAPE_OVAL:
                default_pad = {"padType": "OVAL", "width": w, "height": h}
            elif shape in (pcbnew.PAD_SHAPE_ROUNDRECT, pcbnew.PAD_SHAPE_CHAMFERED_RECT):
                # EDA 没有"圆角矩形"形状；RECT 的 radius 是百分比（实测存 25 = 25%）
                default_pad = {"padType": "RECT", "width": w, "height": h,
                               "radius": round(pad.GetRoundRectRadiusRatio() * 100, 2)}
            else:
                default_pad = {"padType": "RECT", "width": w, "height": h, "radius": 0}
            if hasattr(pad, "GetFPRelativePosition"):
                p0 = pad.GetFPRelativePosition()
                px, py = mil(p0.x / 1e6), -mil(p0.y / 1e6)
                if is_bottom:
                    py = -py
            else:
                px, py = rel(pad.GetPosition())
            plated = pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH
            on_f = pad.IsOnLayer(pcbnew.F_Cu)
            on_b = pad.IsOnLayer(pcbnew.B_Cu)
            if hole or (on_f and on_b):
                pad_layer = 12
            elif is_bottom:
                # 底面封装：EDA 会把整个封装镜像，焊盘层按"顶面等价"写 1
                pad_layer = 1
            elif on_f:
                pad_layer = 1
            elif on_b:
                pad_layer = 2
            else:
                # 仅钢网/阻焊、没有铜的焊盘：封装里不能写 PAD（会让整段封装被丢弃）。
                # 这里只跳过；板级钢网窗口改为稍后**按器件实例**生成（见 F03），
                # 否则同一封装放置多次时只会保留一个窗口。
                continue
            _rel_ang = ((pad.GetOrientationDegrees() - fp.GetOrientationDegrees())
                        if not is_bottom else
                        (fp.GetOrientationDegrees() - pad.GetOrientationDegrees()))
            _pad_angle_deg = round((_rel_ang + slot_extra_angle) % 360.0, 3)
            pid = uuid.uuid4().hex[:16]
            pad_ids[fpid].append(pid)
            pad_pid_by_index[fpid][_pad_index] = pid
            d.add("PAD", pid,
                  # 层归属：有孔=12；双面=12；单面=1/2；无铜（仅钢网/阻焊）=7/8
                  {"groupId": 0, "netName": "", "layerId": pad_layer,
                   "num": pad.GetNumber(), "centerX": px, "centerY": py,
                   # EDA 的 padAngle 是"相对封装"的角度：KiCad 给的是绝对角度，
                   # 必须减去封装角度（否则器件旋转会被叠加两次，焊盘看着转 90°）
                   "padAngle": _pad_angle_deg,
                   "hole": hole,
                   "defaultPad": default_pad, "specialPad": [],
                   "padOffsetX": mil(pad.GetOffset().x / 1e6),
                   "padOffsetY": -mil(pad.GetOffset().y / 1e6),
                   "relativeAngle": slot_rel_angle,
                   "plated": plated,
                   "padType": "NORMAL",
                   "topSolderExpansion": _exp_mil(pad, pcbnew.F_Mask),
                   "bottomSolderExpansion": _exp_mil(pad, pcbnew.B_Mask),
                   "topPasteExpansion": _exp_mil(pad, pcbnew.F_Paste),
                   "bottomPasteExpansion": _exp_mil(pad, pcbnew.B_Paste),
                   "locked": False, "zIndex": 7,
                   "connectMode": pad_connect(bool(hole), pad),
                   "spokeSpace": mil(zone_attr_for(pad)["gap"]) if hole else None,
                   "spokeWidth": mil(zone_attr_for(pad)["spoke"]) if hole else None,
                   "spokeAngle": None, "padLen": 0, "unusedInnerLayers": [],
                   "attrsMap": {}, "propagationDelay": 0})

        d.add("ATTR", uuid.uuid4().hex[:16],
              {"groupId": 0, "parentId": "", "layerId": 3, "x": None, "y": None,
               "key": "Footprint", "value": fpid, "keyVisible": False,
               "valueVisible": False, "fontFamily": "default", "fontSize": 67.5,
               "strokeWidth": 6, "bold": False, "italic": False,
               "origin": "LEFT_BOTTOM", "angle": 0, "reverse": False, "expansion": 0,
               "mirror": False, "locked": False, "zIndex": 15})
        d.add("ATTR", uuid.uuid4().hex[:16],
              {"groupId": 0, "parentId": "", "layerId": 3, "x": None, "y": None,
               "key": "Designator", "value": "U?", "keyVisible": False,
               "valueVisible": False, "fontFamily": "default", "fontSize": 67.5,
               "strokeWidth": 6, "bold": False, "italic": False,
               "origin": "LEFT_BOTTOM", "angle": 0, "reverse": False, "expansion": 0,
               "mirror": False, "locked": False, "zIndex": 11})
        segments.append(doc_stream("FOOTPRINT", d.lines, fp_uuid[fpid]))

    # ---------- DEVICE 段 ----------
    for _fkey in fps:
        fpid = _fkey
        d = Doc()
        d.add("META", "META", {"title": fp_title[_fkey], "description": "", "tags": [], "source": "",
                               "images": [],
                               "attributes": {"Symbol": "", "Footprint": fp_uuid[fpid]}})
        segments.append(doc_stream("DEVICE", d.lines, dev_uuid[fpid]))

    # ---------- PCB 段 ----------
    pcb_uuid = uuid.uuid4().hex[:16]
    pcb = Doc()
    board_uuid = uuid.uuid4().hex[:16]
    pcb.add("META", "META", {"title": name, "parent": "", "source": "",
                             "board": board_uuid, "zIndex": None})
    emit_boiler(pcb, skip=("LAYER", "LAYER_PHYS", "ACTIVE_LAYER"))

    # 层表（只启用实际用到的层）
    for lid in range(1, 61):
        pass
    # 用模板里的 LAYER 记录按 id 输出
    layer_recs = [(h, b) for h, b in boiler if h.get("type") == "LAYER"]
    for head, body in layer_recs:
        lid = body.get("layerId")
        b = dict(body)
        b["use"] = lid in used
        b["show"] = lid in used
        pcb.ticket += 1
        h = dict(head)
        h["ticket"] = pcb.ticket
        pcb.lines.append(rec(h, b))
    for head, body in [(h, b) for h, b in boiler if h.get("type") == "LAYER_PHYS"]:
        pcb.ticket += 1
        h = dict(head)
        h["ticket"] = pcb.ticket
        pcb.lines.append(rec(h, body))

    pcb.add("NET", ["NET", ""], {"netType": None, "specialColor": None, "retLine": True,
                                 "differentialName": None, "isPositiveNet": False,
                                 "equalLengthGroupName": None})
    for n in nets[1:]:
        pcb.add("NET", ["NET", n], {"netType": None, "specialColor": None, "retLine": True,
                                    "differentialName": None, "isPositiveNet": False,
                                    "equalLengthGroupName": None})

    # 器件 + 位号（顺带记录每个焊盘的网络，供导入后修正 PAD_NET）
    pad_nets: dict[str, dict[str, str]] = {}
    for fp in board.GetFootprints():
        if ONLY_REF and fp.GetReference() != ONLY_REF:
            continue
        fpid = (fp.GetFPIDAsString(), fp_signature(fp))
        pos = fp.GetPosition()
        cid = hashlib.md5(("comp:" + fp.GetReference()).encode("utf-8")).hexdigest()[:16]
        pad_nets.setdefault(cid, {})
        dev = {"uuid": dev_uuid[fpid], "name": fp_title[fpid], "source": ""}
        pcb.add("COMPONENT", cid,
                {"partitionId": "", "groupId": 0,
                 "layerId": 2 if fp.GetLayer() == pcbnew.B_Cu else 1,
                 "x": mil(pos.x / 1e6), "y": -mil(pos.y / 1e6),
                 "angle": round(fp.GetOrientationDegrees() % 360.0, 3),
                 "attrs": {"Reuse Block": "", "Group ID": "", "Channel ID": "",
                           "Unique ID": fp.GetReference(),
                           "DeviceName": json.dumps(dev, ensure_ascii=False,
                                                    separators=(",", ":"))},
                 "locked": False, "zIndex": -1, "pinSwap": False,
                 "footprintPrimitives": True,
                 "pinSwapInfo": {cid + padId: {"pinClass": "",
                                               "differentialPairClass": ""}
                                 for padId in pad_ids.get(fpid, [])}})

        for pad in fp.Pads():
            net = pad.GetNetname()
            if not net:
                continue
            key = str(pad.GetNumber()) or "__nonum__"
            pad_nets[cid][key] = net
        pcb.add("ATTR", uuid.uuid4().hex[:16],
                {"partitionId": "", "groupID": 0, "parentId": cid, "layerId": 3,
                 "x": None, "y": None, "key": "Footprint", "value": fp_uuid[fpid],
                 "keyVisible": False, "valueVisible": False, "fontFamily": "default",
                 "fontSize": 45, "strokeWidth": 6, "bold": 0, "italic": 0,
                 "origin": "LEFT_BOTTOM", "angle": 0, "reverse": False, "expansion": 0,
                 "mirror": False, "locked": False, "zIndex": 15})
        # PAD_NET 与 COMPONENT/ATTR 同生命周期。
        # 关键（N01）：id 的第 4 段 padId 指向**封装内那枚焊盘**，
        # 否则同号多焊盘（USB 屏蔽壳、测试点）只能共享一条记录，网络会被互相覆盖。
        for _pad_i, pad in enumerate(fp.Pads()):
            net = pad.GetNetname()
            if not net:
                continue
            num = str(pad.GetNumber())
            pid = pad_pid_by_index.get(fpid, {}).get(_pad_i, "")
            if not pid:
                # 没有 padId 就无法把网络绑到这一枚物理焊盘（N01），宁可报错也不静默覆盖
                sys.exit(f"焊盘网络无法精确映射：{fp.GetReference()} pad {num} 缺少封装内 padId")
            pcb.ticket += 1
            pcb.lines.append(rec(
                {"type": "PAD_NET", "ticket": pcb.ticket,
                 "id": json.dumps(["PAD_NET", cid, num, pid], separators=(",", ":"))},
                {"partitionId": "", "padNet": net, "padLen": None,
                 "propagationDelay": None, "attrsMap": {}}))

        # 位号/值：ATTR 只做语义绑定（坐标置空，避免导入失败），
        # 可见文字用板级 STRING 精确落位（位置/字号/角度/可见性都照 KiCad）
        pcb.add("ATTR", uuid.uuid4().hex[:16],
                {"partitionId": "", "groupID": 0, "parentId": cid, "layerId": 3,
                 "x": None, "y": None, "key": "Designator", "value": fp.GetReference(),
                 "keyVisible": False, "valueVisible": False, "fontFamily": "default",
                 "fontSize": 45, "strokeWidth": 6, "bold": 0, "italic": 0,
                 "origin": "LEFT_BOTTOM", "angle": 0, "reverse": False, "expansion": 0,
                 "mirror": False, "locked": False, "zIndex": 16})

        for _txt in (fp.Reference(), fp.Value()):
            try:
                if not _txt.IsVisible():
                    continue
                tl = _txt.GetLayer()
                if tl not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                    continue
                tp = _txt.GetPosition()
                hgt = _txt.GetTextHeight() / 1e6
                thk = _txt.GetTextThickness() / 1e6
                pcb.add("STRING", uuid.uuid4().hex[:16],
                        {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                         "layerId": 3 if tl == pcbnew.F_SilkS else 4,
                         "x": mil(tp.x / 1e6), "y": -mil(tp.y / 1e6),
                         "text": _txt.GetText(),
                         "fontFamily": eda_font(_txt), "fontSize": mil(hgt),
                         "strokeWidth": mil(thk if thk > 0 else hgt / 8.0),
                         "bold": 0, "italic": 0, "origin": "CENTER_MIDDLE",
                         "angle": round(_txt.GetTextAngleDegrees() % 360.0, 3),
                         "reverse": False, "expansion": 0,
                         "mirror": tl == pcbnew.B_SilkS, "specialColor": None})
            except Exception:
                continue

    # 走线 / 过孔
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            pos = t.GetPosition()
            drill_mil = mil(t.GetDrillValue() / 1e6)
            via_dia = mil(t.GetWidth(t.TopLayer()) / 1e6)
            if ENLARGE_VIA:                          # 默认不改几何，靠规则解决
                min_ring = mil(0.13)
                if (via_dia - drill_mil) / 2.0 < min_ring:
                    via_dia = round(drill_mil + 2 * min_ring, 4)
            pcb.add("VIA", uuid.uuid4().hex[:16],
                    {"partitionId": "", "groupId": "0", "locked": False, "zIndex": -1,
                     "netName": t.GetNetname(), "ruleName": "",
                     "centerX": mil(pos.x / 1e6), "centerY": -mil(pos.y / 1e6),
                     "holeDiameter": drill_mil,
                     "viaDiameter": via_dia,
                     "viaType": "NORMAL", "topSolderExpansion": None,
                     "bottomSolderExpansion": None, "unusedInnerLayers": []})
            continue
        if isinstance(t, pcbnew.PCB_ARC):
            pts = arc_points(t)
            segs = list(zip(pts[:-1], pts[1:]))
        else:
            segs = [(t.GetStart(), t.GetEnd())]
        for a, b in segs:
            pcb.add("LINE", uuid.uuid4().hex[:16],
                    {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                     "netName": t.GetNetname(),
                     "layerId": lm.get(t.GetLayer()),
                     "startX": mil(a.x / 1e6), "startY": -mil(a.y / 1e6),
                     "endX": mil(b.x / 1e6), "endY": -mil(b.y / 1e6),
                     "width": mil(t.GetWidth() / 1e6)})

    knockout_texts = []      # 反显文字：导入后需要再补一次"反相"

    # 板级丝印文字 → STRING（转换器同样处理，38 条这类文本）
    def justify(txt):
        """KiCad 对齐 → EDA origin。

        注意：**镜像文字**（底层丝印）在 EDA 里仍从锚点向 +x 方向排字，
        而 KiCad 镜像后是向 -x 排；因此镜像时把水平对齐左右互换。
        """
        h = txt.GetHorizJustify()
        v = txt.GetVertJustify()
        hm = {pcbnew.GR_TEXT_H_ALIGN_LEFT: "LEFT",
              pcbnew.GR_TEXT_H_ALIGN_CENTER: "CENTER",
              pcbnew.GR_TEXT_H_ALIGN_RIGHT: "RIGHT"}.get(h, "CENTER")
        vm = {pcbnew.GR_TEXT_V_ALIGN_TOP: "TOP",
              pcbnew.GR_TEXT_V_ALIGN_CENTER: "MIDDLE",
              pcbnew.GR_TEXT_V_ALIGN_BOTTOM: "BOTTOM"}.get(v, "MIDDLE")
        # 不再做左右互换：EDA 的底层文字本身就从锚点向 -x 排（与 KiCad 的镜像文字一致），
        # 互换反而会把整块文字推到锚点右侧（实测位置偏一个文字宽度）。
        return f"{hm}_{vm}"

    for d in board.GetDrawings():
        # 板级图形（丝印/铜层）——早期只处理文字，导致 C3 的大圆弧+矩形丝印整块丢失
        if isinstance(d, pcbnew.PCB_SHAPE):
            li = d.GetLayer()
            _is_silk = li in (pcbnew.F_SilkS, pcbnew.B_SilkS)
            _is_copper = pcbnew.IsCopperLayer(li)
            if not (_is_silk or _is_copper):
                continue
            _target = (3 if li == pcbnew.F_SilkS else 4) if _is_silk else lm.get(li)
            if d.GetShape() == pcbnew.SHAPE_T_POLY:
                _poly = d.GetPolyShape()
                if _poly.OutlineCount() == 0:
                    continue
                for _oi in range(_poly.OutlineCount()):
                    _ch = _poly.Outline(_oi)
                    _pts = [(mil(_ch.CPoint(i).x / 1e6), -mil(_ch.CPoint(i).y / 1e6))
                            for i in range(_ch.PointCount())]
                    if len(_pts) < 3:
                        continue
                    _filled = bool(d.GetFilled()) if hasattr(d, "GetFilled") else False
                    if _filled:
                        pcb.add("FILL", uuid.uuid4().hex[:16],
                                {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                                 "netName": "", "layerId": _target, "width": 0,
                                 "fillStyle": "SOLID", "path": [_poly_path(_pts, closed=True)]})
                    else:
                        pcb.add("POLY", uuid.uuid4().hex[:16],
                                {"groupId": 0, "netName": "", "layerId": _target,
                                 "width": _item_width_mil(d),
                                 "path": _poly_path(_pts, closed=True), "locked": False,
                                 "zIndex": 4, "polyType": "NORMAL"})
                continue
            # 圆弧仍按折线输出：EDA 不支持线型（虚线/实线）设置，做成填充带反而更难维护
            for _pl in shape_polylines(d):
                _pts = [(mil(pt.x / 1e6), -mil(pt.y / 1e6)) for pt in _pl]
                if len(_pts) < 2:
                    continue
                if _is_silk:
                    _pts, _info4 = simplify_polyline(_pts)
                    if _info4 and _info4.get("degraded"):
                        degraded.append({"doc": "PCB", "where": "board-graphic",
                                         "layer": _target, **_info4})
                pcb.add("POLY", uuid.uuid4().hex[:16],
                        {"groupId": 0, "netName": "", "layerId": _target,
                         "width": _item_width_mil(d),
                         "path": _poly_path(_pts), "locked": False, "zIndex": 4,
                         "polyType": "NORMAL"})
            continue

        if not isinstance(d, pcbnew.PCB_TEXT):
            continue
        li = d.GetLayer()
        if li not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            continue
        txt = d.GetText() or ""
        if not txt.strip():
            continue
        pos = d.GetPosition()
        h = d.GetTextHeight() / 1e6
        th = d.GetTextThickness() / 1e6
        pcb.add("STRING", uuid.uuid4().hex[:16],
                {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                 "layerId": 3 if li == pcbnew.F_SilkS else 4,
                 "x": mil(pos.x / 1e6), "y": -mil(pos.y / 1e6), "text": txt,
                 "fontFamily": eda_font(d), "fontSize": mil(h),
                 "strokeWidth": mil(th if th > 0 else h / 8.0),
                 "bold": 0, "italic": 0, "origin": justify(d),
                 "angle": round(d.GetTextAngleDegrees() % 360.0, 3),
                 # KiCad 的 knockout（反显/镂空）→ EDA 的 reverse + expansion
                 "reverse": bool(d.IsKnockout()) if hasattr(d, "IsKnockout") else False,
                 "expansion": (mil(d.GetTextThickness() / 1e6) * 0.5
                               if (hasattr(d, "IsKnockout") and d.IsKnockout()) else 0),
                 "mirror": False, "specialColor": None})
        if hasattr(d, "IsKnockout") and d.IsKnockout():
            knockout_texts.append({"text": txt,
                                   "x": mil(pos.x / 1e6), "y": -mil(pos.y / 1e6),
                                   "expansion": round(mil(th if th > 0 else h / 8.0), 3)})

    # 禁止区域（KiCad rule area / keepout）→ REGION(PROHIBIT)
    for z in board.Zones():
        if not getattr(z, "GetIsRuleArea", lambda: False)():
            continue
        prohibits = []
        if (getattr(z, "GetDoNotAllowZoneFills", lambda: False)()
                or getattr(z, "GetDoNotAllowCopperPour", lambda: False)()):
            prohibits.append("COPPER")
        if getattr(z, "GetDoNotAllowTracks", lambda: False)():
            prohibits.append("TRACK")
        if getattr(z, "GetDoNotAllowVias", lambda: False)():
            prohibits.append("VIA")
        if getattr(z, "GetDoNotAllowPads", lambda: False)():
            prohibits.append("COMPONENT")
        if getattr(z, "GetDoNotAllowFootprints", lambda: False)() and "COMPONENT" not in prohibits:
            prohibits.append("COMPONENT")
        if not prohibits:
            prohibits = ["COPPER", "TRACK", "VIA"]
        outline = z.Outline()
        if outline.OutlineCount() == 0:
            continue
        chain = outline.Outline(0)
        pts = [(mil(chain.CPoint(i).x / 1e6), -mil(chain.CPoint(i).y / 1e6))
               for i in range(chain.PointCount())]
        if len(pts) < 3:
            continue
        for lid_k in z.GetLayerSet().Seq():
            pcb.add("REGION", uuid.uuid4().hex[:16],
                    {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                     "width": mil(0.254), "prohibitType": prohibits,
                     "path": [_poly_path(pts, closed=True)], "name": "",
                     "regionType": "PROHIBIT",
                     "layerId": lm.get(lid_k)})
        continue

    # 铺铜：POUR（区域）+ POURED（EDA 自己灌的填充）
    # 注意：fill_items / pour_id 必须在循环**外**初始化——否则无铺铜的板会在循环后
    # 读取未定义变量（UnboundLocalError）。SKIP_POURED 时干脆不构造填充。
    fill_items = []
    pour_id = None
    _pour_count = 0
    _skipped_netless = 0
    for z in board.Zones():
        if getattr(z, "GetIsRuleArea", lambda: False)():
            continue
        # 无网络的 zone 不是"铺铜"，多半是设计者用 zone 画的禁布/隔离区。
        # 写进 POUR 会变成有实体填充的无网铜，与焊盘/走线冲突（实测产生 3 条同封装间距错误）。
        if not z.GetNetname():
            _skipped_netless += 1
            continue
        for lid_k in z.GetLayerSet().Seq():
            if not pcbnew.IsCopperLayer(lid_k):
                continue
            outline = z.Outline()
            if outline.OutlineCount() == 0:
                continue
            chain = outline.Outline(0)
            pts = [(mil(chain.CPoint(i).x / 1e6), -mil(chain.CPoint(i).y / 1e6))
                   for i in range(chain.PointCount())]
            if len(pts) < 3:
                continue
            pour_id = uuid.uuid4().hex[:16]
            _pour_count += 1
            pcb.add("POUR", pour_id,
                    {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                     "netName": z.GetNetname(),
                     "layerId": lm.get(lid_k),
                     "width": 0.25, "name": f"POUR_{lid_k}", "order": 0,
                     "path": [_poly_path(pts, closed=True)],
                     "pourType": {"pourType": "SOLID", "fineness": 8},
                     "keepIsland": False})
            if SKIP_POURED:
                continue
            filled = z.GetFilledPolysList(lid_k)
            fill_items = []

            def _fill_pts(chain):
                # POURED 的坐标单位与 POUR 不同（实测 = POUR 坐标 / 10）
                return [(mil(chain.CPoint(i).x / 1e6) / 10.0,
                         -mil(chain.CPoint(i).y / 1e6) / 10.0)
                        for i in range(chain.PointCount())]

            for oi in range(filled.OutlineCount()):
                fc = filled.Outline(oi)
                fpts = _fill_pts(fc)
                if len(fpts) < 3:
                    continue
                # 外环 + 所有内孔（anti-pad）都要带上，否则铜皮会盖住焊盘
                paths = [_poly_path(fpts, closed=True)]
                for hi in range(filled.HoleCount(oi)):
                    hc = filled.Hole(oi, hi)
                    hpts = _fill_pts(hc)
                    if len(hpts) >= 3:
                        paths.append(_poly_path(hpts, closed=True))
                fill_items.append({"id": uuid.uuid4().hex[:16], "strokeWidth": 0,
                                   "fill": True, "path": paths})
    # 铺铜：POUR（区域）+ POURED（EDA 自己灌的填充）
    if _skipped_netless:
        print(f"  ! 跳过 {_skipped_netless} 个无网络的 zone（按禁布/隔离区处理，不生成铺铜）")
    if _pour_count == 0:
        print("  · 本板没有有效铺铜区（POUR=0），后续重铺步骤会标记为“无需执行”")

    if fill_items and pour_id and not SKIP_POURED:
        pcb.ticket += 1
        pcb.lines.append(rec({"type": "POURED", "ticket": pcb.ticket,
                              "id": json.dumps(["POURED", pour_id])},
                             {"pourFill": fill_items}))

    # 板框
    edges = []
    for d in board.GetDrawings():
        if d.GetLayer() != pcbnew.Edge_Cuts:
            continue
        shape = d.GetShape()
        if shape == pcbnew.SHAPE_T_SEGMENT:
            edges.append((d.GetStart(), d.GetEnd()))
        elif shape == pcbnew.SHAPE_T_RECT:
            a, b = d.GetStart(), d.GetEnd()
            corners = [a, pcbnew.VECTOR2I(b.x, a.y), b, pcbnew.VECTOR2I(a.x, b.y)]
            for i in range(4):
                edges.append((corners[i], corners[(i + 1) % 4]))
        elif shape in (pcbnew.SHAPE_T_ARC, pcbnew.SHAPE_T_CIRCLE):
            for pts in shape_polylines(d):
                for i in range(len(pts) - 1):
                    edges.append((pts[i], pts[i + 1]))
    loops = _chain_edges(edges)
    if not loops:
        print(f"  ! 警告：Edge.Cuts 有 {len(edges)} 条边，但没能串成闭合环——板框未生成")
    elif len(loops) > 1:
        print(f"  ! 提示：Edge.Cuts 串出 {len(loops)} 个环（可能有镂空/多块板）")
    for loop in loops:
        pts = [(mil(p.x / 1e6), -mil(p.y / 1e6)) for p in loop]
        # 开链兜底时首尾可能差几微米：按 EDA 要求补成闭合路径
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        pcb.add("POLY", uuid.uuid4().hex[:16],
                {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                 "netName": "", "layerId": 11, "width": 10,
                 "path": _poly_path(pts, closed=True), "polyType": "BOARD_OUTLINE"})

    # 仅钢网焊盘（如 ESP32 散热盘的独立印膏窗口）→ 板级锡膏层实心填充
    # 注意：POLY 是折线（stroke），不能表达实心开窗，必须用 FILL + fillStyle SOLID
    # 关键（F03）：这里按**器件实例**逐个生成，不能用去重后的封装库定义，
    # 否则同一封装放置 N 次只会保留代表实例的那一个窗口。
    _paste_seen = set()
    for _fp_inst in board.GetFootprints():
        if ONLY_REF and _fp_inst.GetReference() != ONLY_REF:
            continue
        for _p in _fp_inst.Pads():
            if _p.IsOnLayer(pcbnew.F_Cu) or _p.IsOnLayer(pcbnew.B_Cu):
                continue
            _pls = []
            if _p.IsOnLayer(pcbnew.F_Paste):
                _pls.append(7)
            if _p.IsOnLayer(pcbnew.B_Paste):
                _pls.append(8)
            if not _pls:
                continue
            # 关键（N03）：钢网窗口必须按焊盘的**实际形状**生成，不能一律外接矩形
            # （1mm 圆垫用方窗 = 面积 +27.3%，属实际制造几何变化）。
            _pts = _pad_window_polygon(_p)
            if not _pts:
                continue
            _key = tuple((round(x, 1), round(y, 1)) for x, y in _pts)
            for _lid in _pls:
                if (_lid, _key) in _paste_seen:
                    continue
                _paste_seen.add((_lid, _key))
                paste_windows.append((_lid, list(_pts)))
    for _lid, _pts in paste_windows:
        pcb.add("FILL", uuid.uuid4().hex[:16],
                {"partitionId": "", "groupId": 0, "locked": False, "zIndex": -1,
                 "netName": "", "layerId": _lid, "width": 0,
                 "fillStyle": "SOLID", "path": [_poly_path(_pts, closed=True)]})

    segments.append(doc_stream("PCB", pcb.lines, pcb_uuid))

    # ---------- 层映射自检：无法映射的层默认阻断（避免铜被静默放进文档层）----------
    _problems = lm.problems()
    if _problems:
        msg = ("以下层无法映射到 EDA 层（会造成铜/丝印错位）：" + "、".join(_problems)
               + "；确认可接受后加 --allow-unknown-layer 继续")
        if getattr(build, "allow_unknown_layer", False):
            print("  ! " + msg)
            print("  ! 已按 --allow-unknown-layer 继续，这些对象被放到文档层 13")
        else:
            sys.exit("层映射失败：" + msg)

    # ---------- 写工程文件夹 ----------
    proj_dir = os.path.join(out_dir, name + "_eprj3")
    os.makedirs(os.path.join(proj_dir, "pcb"), exist_ok=True)
    with open(os.path.join(proj_dir, "pcb", "PCB1.epcb2"), "w", encoding="utf-8") as fh:
        fh.write("".join(segments))

    # 反显文字清单：EDA 导入会丢"反相"标志，需要用 tools/eda_apply_knockout.py 补写
    if degraded:
        with open(os.path.join(proj_dir, "degraded_polylines.json"), "w", encoding="utf-8") as fh:
            json.dump(degraded, fh, ensure_ascii=False, indent=1)
        print(f"  ! {len(degraded)} 条丝印折线简化未达标（已写入 degraded_polylines.json）")
    with open(os.path.join(proj_dir, "knockout_texts.json"), "w", encoding="utf-8") as fh:
        json.dump(knockout_texts, fh, ensure_ascii=False, indent=1)
    if knockout_texts:
        print(f"  反显文字：{len(knockout_texts)} 条（导入后由 eda_apply_knockout.py 补写反相）")

    index = {
        "name": name,
        "owner_uuid": uuid.uuid4().hex[:16], "creator_uuid": uuid.uuid4().hex[:16],
        "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        "modifier_uuid": uuid.uuid4().hex[:16], "content": "", "archive": False,
        "cbb_project": 0, "thumb": "", "ticket": 1, "g_ticket": 1, "boards": [],
        "block_symbol_attrs_groups": {}, "default_sheet": "", "branch_uuid": "",
        "pcb_count": 1, "format": "folder",
        "profile": {
            "boards": {board_uuid: {"uuid": board_uuid, "title": "Board1", "zIndex": 1}},
            "schematics": {}, "sheets": {},
            "pcbs": {pcb_uuid: {"uuid": pcb_uuid, "title": "PCB1", "board": board_uuid,
                                "parent_uuid": "", "source": "", "version": "1",
                                "updateTime": 0}},
            "panels": {}, "blockSymbols": {},
            "owner": {"uuid": uuid.uuid4().hex[:16]},
            "simSchematics": {}, "simulations": {},
        },
        "config": {"defaultSheet": "", "settings": {}},
    }
    with open(os.path.join(proj_dir, name + ".eprj3"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    with open(os.path.join(proj_dir, "pad_nets.json"), "w", encoding="utf-8") as fh:
        json.dump(pad_nets, fh, ensure_ascii=False, indent=1)

    print(f"生成：{os.path.join(proj_dir, name + '.eprj3')}")
    print(f"  焊盘网络表：{len(pad_nets)} 个器件 → pad_nets.json")
    print(f"  唯一封装 {len(fps)} 个（FOOTPRINT+DEVICE 段各 {len(fps)}）")
    print(f"  记录：{sum(len([l for l in s.splitlines() if l.strip()]) for s in segments)} 行")
    return proj_dir


def main(argv=None):
    ap = argparse.ArgumentParser(description="KiCad 板 → 嘉立创EDA .eprj3 文件夹工程（路线 A）")
    ap.add_argument("board")
    ap.add_argument("-o", "--out", default="out/eprj3")
    ap.add_argument("--name", default=None)
    ap.add_argument("--only-ref", default=None, help="只生成该位号的器件（调试用）")
    ap.add_argument("--enlarge-via", action="store_true",
                    help="把过孔外径放大到环宽 0.13mm（默认不放大，保持与 KiCad 一致）")
    ap.add_argument("--value-attr", action="store_true",
                    help="额外写 Value 属性（实测会让导入失败，默认关闭）")
    ap.add_argument("--with-poured", action="store_true",
                    help="写入 POURED 填充（默认不写，交给 EDA 自己灌铜）")
    ap.add_argument("--allow-unknown-layer", action="store_true",
                    help="层无法映射时仍然继续（默认阻断，避免铜被放进文档层）")
    ap.add_argument("--template", default=None,
                    help="转换器产出的 .epcb2（提供 PRIMITIVE/LAYER/RULE 等 boilerplate）")
    a = ap.parse_args(argv)
    global ONLY_REF, SKIP_POURED, ENLARGE_VIA, VALUE_ATTR
    ONLY_REF = a.only_ref
    SKIP_POURED = not a.with_poured
    ENLARGE_VIA = a.enlarge_via
    VALUE_ATTR = a.value_attr
    name = a.name or os.path.splitext(os.path.basename(a.board))[0]
    build.allow_unknown_layer = a.allow_unknown_layer
    build(a.board, a.out, name, a.template)
    return 0


if __name__ == "__main__":
    sys.exit(main())
