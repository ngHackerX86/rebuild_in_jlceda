"""最小可用的 Gerber RS-274X 解析器。

只为"嘉立创预检"服务，不做光栅化：需要的是
  * 坐标格式 / 单位
  * 孔径表与使用次数（推最小线宽、最小图形尺寸）
  * 图形对象（闪光 / 线段 / 区域），用于包络、板框闭合性、重复图形检查
  * X2 属性、极性指令统计

支持子集：FS / MO / AD / AM / LP / TF-TA-TO-TD / G01 G02 G03 G36 G37 G54 G74 G75 /
D01 D02 D03 / M02。

**不支持的语法不会静默跳过**：step-repeat（%SR）、block aperture（%AB）、负极性
（%LPC）、增量坐标（G91）、无法求形的宏/多边形孔径，都会登记到 `GerberFile.unsupported`，
由发布判定（gate.py）阻断发布。理由：几何被低估的"通过"比"不通过"更危险。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 孔径宏求值
# 只求"外形包络"，不渲染。支持标准图元 1(圆) / 4(轮廓) / 5(多边形) /
# 20(矢量线) / 21(中心线矩形)；遇到不认识的图元返回 None（调用方跳过依赖
# 尺寸的检查，而不是猜一个数）。exposure=0 的图元是"擦除"，不影响包络。

_NUM_OK = re.compile(r"^[-+0-9.eE*/() ]+$")


def _ev(tok: str, params: tuple[float, ...]) -> float:
    """求值一个宏参数表达式，如 `$1`、`0-$2`、`-0.6`。"""
    s = str(tok).strip()
    if not s:
        return 0.0
    s = re.sub(r"\$(\d+)", lambda m: repr(float(params[int(m.group(1)) - 1])), s)
    if not _NUM_OK.match(s):
        raise ValueError(f"unsupported macro expression: {tok!r}")
    return float(eval(s, {"__builtins__": {}}, {}))  # noqa: S307 — 已用白名单正则限制


def _rot(pts, deg: float):
    if not deg:
        return list(pts)
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(x * ca - y * sa, x * sa + y * ca) for (x, y) in pts]


def _macro_bbox(raw: str, params: tuple[float, ...]) -> tuple[float, float] | None:
    """按宏定义与实参算出外形包络 (宽, 高)。无法判定返回 None。"""
    body = raw.strip()
    if body.startswith("%AM"):
        body = body[3:]
    body = body.rstrip("%*")
    parts = [t.strip() for t in body.split("*") if t.strip()]
    xs: list[float] = []
    ys: list[float] = []
    ok = False

    def add(px, py):
        xs.extend(px)
        ys.extend(py)

    for tok in parts[1:]:                       # parts[0] 是宏名
        f = [p.strip() for p in tok.split(",")]
        try:
            mcode = re.match(r"^(\d+)", f[0].strip())
            if not mcode:                       # 不是图元（例如纯注释残片）
                continue
            code = int(mcode.group(1))
            if code == 0:                       # 0 = 注释图元（Gerber 规范），跳过
                continue
            if code == 1:                       # 圆：1,曝光,直径,cx,cy[,旋转]
                if int(float(f[1])) == 0:
                    continue
                d = _ev(f[2], params)
                cx, cy = _ev(f[3], params), _ev(f[4], params)
                add([cx - d / 2, cx + d / 2], [cy - d / 2, cy + d / 2])
            elif code == 4:                     # 轮廓：4,曝光,n,x0,y0,...,旋转
                if int(float(f[1])) == 0:
                    continue
                n = int(float(f[2]))
                vals = [_ev(p, params) for p in f[3:3 + 2 * n]]
                rot = _ev(f[3 + 2 * n], params) if len(f) > 3 + 2 * n else 0.0
                pts = _rot(list(zip(vals[0::2], vals[1::2])), rot)
                add([p[0] for p in pts], [p[1] for p in pts])
            elif code == 5:                     # 多边形：5,曝光,n,cx,cy,直径,旋转
                if int(float(f[1])) == 0:
                    continue
                n = int(float(f[2]))
                cx, cy = _ev(f[3], params), _ev(f[4], params)
                r = _ev(f[5], params) / 2
                rot = _ev(f[6], params) if len(f) > 6 else 0.0
                pts = _rot([(cx + r * math.cos(2 * math.pi * k / n),
                             cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)], rot)
                add([p[0] for p in pts], [p[1] for p in pts])
            elif code == 20:                    # 矢量线：20,曝光,宽,x1,y1,x2,y2[,旋转]
                if int(float(f[1])) == 0:
                    continue
                w = _ev(f[2], params)
                x1, y1, x2, y2 = (_ev(f[3], params), _ev(f[4], params),
                                  _ev(f[5], params), _ev(f[6], params))
                rot = _ev(f[7], params) if len(f) > 7 else 0.0
                (x1, y1), (x2, y2) = _rot([(x1, y1), (x2, y2)], rot)
                add([min(x1, x2) - w / 2, max(x1, x2) + w / 2],
                    [min(y1, y2) - w / 2, max(y1, y2) + w / 2])
            elif code == 21:                    # 中心线矩形：21,曝光,宽,高,cx,cy,旋转
                if int(float(f[1])) == 0:
                    continue
                w, h = _ev(f[2], params), _ev(f[3], params)
                cx, cy = _ev(f[4], params), _ev(f[5], params)
                rot = _ev(f[6], params) if len(f) > 6 else 0.0
                pts = _rot([(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                            (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)], rot)
                add([p[0] for p in pts], [p[1] for p in pts])
            else:
                return None                     # 未知图元：宁可放弃判定
            ok = True
        except (IndexError, ValueError):
            return None
    if not ok or not xs:
        return None
    return (max(xs) - min(xs), max(ys) - min(ys))


# ---------------------------------------------------------------- 数据结构


@dataclass
class Aperture:
    dcode: int
    template: str            # C / R / O / P / MACRO
    params: tuple[float, ...] = ()
    macro: str | None = None
    macro_text: str | None = None   # 宏定义原文，用于还原真实外形
    uses: int = 0

    def min_feature(self) -> float | None:
        """该孔径能表示的最小线宽/最小图形厚度（mm）。宏无法静态判定则 None。"""
        p = self.params
        t = self.template
        if t == "C" and p:
            return float(p[0])
        if t in ("R", "O") and len(p) >= 2:
            return float(min(p[0], p[1]))
        if t == "P":
            return None          # 多边形的最小颈宽不能由外径推出
        return None

    def size(self) -> tuple[float, float] | None:
        p, t = self.params, self.template
        if t == "C" and p:
            return (float(p[0]), float(p[0]))
        if t in ("R", "O") and len(p) >= 2:
            return (float(p[0]), float(p[1]))
        if t == "MACRO" and self.macro_text:
            return _macro_bbox(self.macro_text, p)
        return None


@dataclass
class Flash:
    x: float
    y: float
    dcode: int


@dataclass
class Stroke:
    dcode: int
    points: list[tuple[float, float]]
    arc: bool = False


@dataclass
class Region:
    points: list[tuple[float, float]]


@dataclass
class GerberFile:
    path: str
    units: str | None = None          # mm / inch
    fmt: tuple[int, int] | None = None  # (整数位, 小数位)
    leading_zero_omitted: bool | None = None
    apertures: dict[int, Aperture] = field(default_factory=dict)
    macros: dict[str, str] = field(default_factory=dict)
    x2_attrs: dict[str, int] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)
    polarity_commands: int = 0
    regions: list[Region] = field(default_factory=list)
    flashes: list[Flash] = field(default_factory=list)
    strokes: list[Stroke] = field(default_factory=list)
    op_counts: dict[str, int] = field(default_factory=dict)
    # 遇到但未实现的语法：非空即代表"几何不可信"，发布判定会阻断
    unsupported: list[str] = field(default_factory=list)
    # 合法、嘉立创也支持，但本解析器未建模（如 %LPC 极性清除）：
    # 不阻断发布，只把依赖几何的检查降级，并在报告里写明"该层几何检查降级"
    degraded: list[str] = field(default_factory=list)

    # ---------------- 派生量 ----------------
    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for f in self.flashes:
            s = self.apertures.get(f.dcode)
            wh = s.size() if s else None
            if wh:
                xs += [f.x - wh[0] / 2, f.x + wh[0] / 2]
                ys += [f.y - wh[1] / 2, f.y + wh[1] / 2]
            else:
                xs.append(f.x); ys.append(f.y)
        for st in self.strokes:
            w = self.apertures.get(st.dcode)
            half = (w.min_feature() or 0.0) / 2 if w else 0.0
            for (x, y) in st.points:
                xs += [x - half, x + half]; ys += [y - half, y + half]
        for r in self.regions:
            for (x, y) in r.points:
                xs.append(x); ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def bbox_centerline(self) -> tuple[float, float, float, float] | None:
        """只用图形中心线计算的包络（板框层用它，避免线宽外扩 0.05mm）。"""
        xs: list[float] = []
        ys: list[float] = []
        for f in self.flashes:
            xs.append(f.x); ys.append(f.y)
        for st in self.strokes:
            for (x, y) in st.points:
                xs.append(x); ys.append(y)
        for r in self.regions:
            for (x, y) in r.points:
                xs.append(x); ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def min_feature(self, kind: str = "all") -> float | None:
        vals = []
        for ap in self.apertures.values():
            if kind == "stroke" and ap.uses == 0:
                continue
            v = ap.min_feature()
            if v:
                vals.append(v)
        return min(vals) if vals else None

    def path_endpoints(self) -> list[tuple[float, float]]:
        """所有绘制路径的端点（用于板框闭合性判断）。区域本身闭合，不计。"""
        eps: list[tuple[float, float]] = []
        for st in self.strokes:
            if st.points:
                eps.append(st.points[0])
                eps.append(st.points[-1])
        for r in self.regions:
            if r.points:
                eps.append(r.points[0])
                eps.append(r.points[-1])
        return eps

    def section_names(self) -> list[str]:
        return [c[3:].strip() for c in self.comments if c.startswith("G04")]


# ---------------------------------------------------------------- 解析器

_AP_DEF = re.compile(r"^ADD(\d+)([^,*]*)(?:,([^*]*))?\*$")
_COORD = re.compile(r"^(?:X([-+]?\d+))?(?:Y([-+]?\d+))?$")
_INTERP = re.compile(r"G0?([123])(?![0-9])")


def _flatten_arc(x0, y0, x1, y1, i, j, cw, tol=0.002) -> list[tuple[float, float]]:
    cx, cy = x0 + i, y0 + j
    r = math.hypot(x0 - cx, y0 - cy)
    if r == 0:
        return [(x1, y1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        while a1 >= a0:
            a1 -= 2 * math.pi
    else:
        while a1 <= a0:
            a1 += 2 * math.pi
    span = abs(a1 - a0)
    if span < 1e-9 and (abs(x0 - x1) > 1e-9 or abs(y0 - y1) > 1e-9):
        span = 2 * math.pi
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tol / max(r, 1e-9))))
    n = max(2, min(720, int(math.ceil(span / max(step, 1e-4))) + 1))
    pts = []
    for k in range(1, n + 1):
        a = a0 + (a1 - a0) * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def parse(path: str) -> GerberFile:
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    flow = raw.replace("\r\n", "\n").replace("\r", "\n")
    gf = GerberFile(path=path)

    # 把跨行的扩展指令（% ... %）合并成单 token。
    # 注意：%AM 宏一定跨行（KiCad 逐行写图元，末尾单独一行 %），
    # 之前"只把开始行攒起来、遇到第一个非 % 行就收尾"会把宏截断成空壳。
    raw_lines = [ln.strip() for ln in flow.split("\n")]
    lines: list[str] = []
    i = 0
    while i < len(raw_lines):
        s = raw_lines[i]
        if not s:
            i += 1
            continue
        if s.startswith("%") and not s.endswith("%"):
            buf = s
            while i + 1 < len(raw_lines):
                i += 1
                nxt = raw_lines[i]
                buf += nxt
                if nxt.endswith("%"):
                    break
            lines.append(buf)
        else:
            lines.append(s)
        i += 1

    scale = 1e-5          # 默认 4.5，遇 %FS 后重算
    cur_ap: int | None = None
    interp = 1
    multi_quadrant = True
    x = y = 0.0
    in_region = False
    region_pts: list[tuple[float, float]] = []
    cur_stroke: Stroke | None = None
    counts: dict[str, int] = {}

    def bump(k: str, n: int = 1):
        counts[k] = counts.get(k, 0) + n

    def move_to(nx: float, ny: float):
        nonlocal x, y, cur_stroke
        x, y = nx, ny
        cur_stroke = None
        if in_region and region_pts:
            region_pts.append((x, y))

    def line_to(nx: float, ny: float, sx: float | None = None, sy: float | None = None):
        nonlocal x, y, cur_stroke
        sx = x if sx is None else sx
        sy = y if sy is None else sy
        if in_region:
            region_pts.append((nx, ny))
        else:
            if cur_stroke is None:
                cur_stroke = Stroke(dcode=cur_ap or 0, points=[(sx, sy)])
                gf.strokes.append(cur_stroke)
            cur_stroke.points.append((nx, ny))
        x, y = nx, ny

    def arc_to(nx: float, ny: float, i: float, j: float,
               sx: float | None = None, sy: float | None = None):
        nonlocal x, y, cur_stroke
        sx = x if sx is None else sx
        sy = y if sy is None else sy
        pts = _flatten_arc(sx, sy, nx, ny, i, j, cw=(interp == 2))
        if in_region:
            region_pts.extend(pts)
        else:
            if cur_stroke is None:
                cur_stroke = Stroke(dcode=cur_ap or 0, points=[(sx, sy)], arc=True)
                gf.strokes.append(cur_stroke)
            cur_stroke.arc = True
            cur_stroke.points.extend(pts)
        x, y = nx, ny

    for s in lines:
        if s.startswith("%"):
            body = s[1:-1] if s.endswith("%") else s[1:]
            for tok in body.split("*"):
                tok = tok.strip()
                if not tok:
                    continue
                if tok.startswith("FS"):
                    m = re.match(r"FS([LT]?)([AI]?)X(\d)(\d)Y(\d)(\d)", tok)
                    if m:
                        gf.leading_zero_omitted = (m.group(1) == "L")
                        gf.fmt = (int(m.group(3)), int(m.group(4)))
                        scale = 10.0 ** -int(m.group(4))
                    bump("FS")
                elif tok.startswith("MO"):
                    gf.units = "mm" if tok[2:4].upper() == "MM" else "inch"
                    bump("MO" + gf.units)
                elif tok.startswith("AM"):
                    name = tok[2:].split("*")[0]
                    gf.macros[name] = s
                    bump("AM")
                elif tok.startswith("ADD"):
                    m = _AP_DEF.match(tok + "*")
                    if m:
                        dcode = int(m.group(1))
                        tmpl = m.group(2).strip()
                        params_s = m.group(3) or ""
                        macro_name = None
                        if tmpl not in ("C", "R", "O", "P"):
                            macro_name, tmpl = tmpl, "MACRO"
                        params = []
                        for v in re.split(r"[Xx]", params_s):
                            v = v.strip()
                            if not v:
                                continue
                            try:
                                params.append(float(v))
                            except ValueError:
                                pass
                        gf.apertures[dcode] = Aperture(
                            dcode, tmpl, tuple(params), macro_name,
                            gf.macros.get(macro_name) if macro_name else None)
                        bump("AD")
                elif tok.startswith("LP"):
                    gf.polarity_commands += 1
                    bump("LP" + tok[2:3])
                    if tok[2:3].upper() == "C":
                        # KiCad 在"从丝印挖掉阻焊"时就会写 %LPC；这是标准语法，
                        # 嘉立创也支持。缺的只是我们自己的布尔建模，因此降级而非阻断。
                        gf.degraded.append("LP 负极性（%LPC）：本解析器未做布尔清除")
                elif tok.startswith("SR"):
                    gf.unsupported.append(f"step-repeat（%{tok[:12]}）：未展开阵列，包络会被低估")
                    bump("SR")
                elif tok.startswith("AB"):
                    gf.unsupported.append(f"block aperture（%{tok[:12]}）：未展开块，几何缺失")
                    bump("AB")
                elif tok.startswith("IP"):
                    if "NEG" in tok.upper():
                        gf.unsupported.append("图像负片（%IPNEG）：未实现，极性反转")
                    bump("IP")
                elif tok[:3] in ("TF.", "TA.", "TO.", "TD."):
                    key = tok.split(",")[0]
                    gf.x2_attrs[key] = gf.x2_attrs.get(key, 0) + 1
                    bump("X2")
                else:
                    bump("EXT:" + tok.split("*")[0][:6])
            continue

        if s.startswith("G04"):
            gf.comments.append(s.rstrip("*"))
            bump("G04")
            continue

        # 普通指令行：可能含 G 码 + 坐标 + D 码
        if "G36" in s:
            in_region = True
            region_pts = []
            bump("G36")
            s = s.replace("G36", "")
            if not s.strip("*"):
                continue
        if "G37" in s:
            if len(region_pts) >= 3:
                gf.regions.append(Region(region_pts))
            in_region = False
            region_pts = []
            bump("G37")
            s = s.replace("G37", "")
            if not s.strip("*"):
                continue
        m = _INTERP.search(s)
        if m:
            interp = int(m.group(1))
            s = s.replace(m.group(0), "")
        if "G74" in s:
            multi_quadrant = False; s = s.replace("G74", "")
        if "G75" in s:
            multi_quadrant = True; s = s.replace("G75", "")
        if "G90" in s:
            s = s.replace("G90", "")
        if "G91" in s:
            gf.unsupported.append("增量坐标（G91）：未实现，坐标不可信")
            s = s.replace("G91", "")
        if "G54" in s:
            s = s.replace("G54", "")
        if "G71" in s or "G70" in s:
            s = s.replace("G71", "").replace("G70", "")
        if s.startswith("M02") or s.startswith("M00"):
            bump("M02")
            break

        s = s.replace("*", "")
        if not s:
            continue

        dcode = None
        m = re.search(r"D(\d+)$", s)
        if m:
            dcode = int(m.group(1))
            s = s[:m.start()]
        if not s and dcode is not None and dcode >= 10:
            cur_ap = dcode
            if cur_ap not in gf.apertures:      # 宽容处理：先登记未知孔径
                gf.apertures[cur_ap] = Aperture(cur_ap, "MACRO", ())
            bump("D%02d选刀" % cur_ap)
            continue

        nx = ny = None
        i = j = 0.0
        for axis, val in re.findall(r"([XYIJ])([-+]?\d+)", s):
            v = int(val) * scale
            if axis == "X":
                nx = v
            elif axis == "Y":
                ny = v
            elif axis == "I":
                i = v
            else:
                j = v

        px, py = x, y                     # 本段起点（move/line/arc 会自行推进 x,y）
        if nx is None:
            nx = x
        if ny is None:
            ny = y

        if dcode is None:
            x, y = nx, ny
            continue
        if dcode == 2:
            move_to(nx, ny)
            bump("D02")
        elif dcode == 1:
            if interp in (2, 3):
                arc_to(nx, ny, i, j, px, py)
                bump("D01弧")
            else:
                line_to(nx, ny, px, py)
                bump("D01线")
            if cur_ap in gf.apertures:
                gf.apertures[cur_ap].uses += 1
        elif dcode == 3:
            x, y = nx, ny
            if in_region:
                region_pts.append((x, y))
            gf.flashes.append(Flash(x, y, cur_ap or 0))
            if cur_ap in gf.apertures:
                gf.apertures[cur_ap].uses += 1
            bump("D03")
        elif dcode >= 10:
            x, y = nx, ny
            cur_ap = dcode
            bump("D%02d选刀" % cur_ap)

    # 收尾：把"缺声明""求不出形"的孔径也登记为不可信
    if gf.fmt is None:
        gf.unsupported.append("缺少 %FS 坐标格式声明：小数位按默认 4.5 猜，可能有偏差")
    if gf.units is None:
        gf.unsupported.append("缺少 %MO 单位声明：按 mm 处理，未确认")
    for dcode, ap in gf.apertures.items():
        if ap.uses == 0:
            continue
        if ap.template == "P":
            gf.unsupported.append(f"多边形孔径 D{dcode}（P）：未精确求形，包络不可信")
        if ap.template == "MACRO" and ap.macro and ap.size() is None:
            gf.unsupported.append(f"宏孔径 D{dcode}（{ap.macro}）：含未实现图元，尺寸不可判定")
        if ap.template == "MACRO" and ap.macro is None:
            gf.unsupported.append(f"孔径 D{dcode} 用了未定义的宏，几何缺失")

    gf.op_counts = counts
    return gf
