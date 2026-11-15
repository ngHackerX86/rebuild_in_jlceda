"""嘉立创下单前预检：把"能不能投"问题在本地拦下来。

检查分三档（评审 S-03/S-06 之后的分层）：
  * 文件级（本模块，纯 Python，可脱离 KiCad 运行）
  * 板级（board_rules.py，需要 pcbnew 或 IPC API）
  * 发布判定（gate.py，把各阶段结论合并；缺阶段 = 不通过）

本模块只对"能从 Gerber/Excellon 里确证的东西"下结论。凡解析器报告
`unsupported`（%SR、%LPC、G91、铣槽模式等），都作为 ERROR 上报，
因为"几何被低估的通过"比"不通过"更危险。
"""
from __future__ import annotations

import itertools
import json
import math
import os
import re
from dataclasses import dataclass, field

from . import excellon, gerber, layers

DEFAULT_RULES = {
    "min_trace_mm": 0.127,          # 允许值（超出范围报警告）
    "comfortable_trace_mm": 0.1524, # 双层常规工艺（6 mil）
    "comfortable_trace_mm_multilayer": 0.1016,  # 4 层及以上常规工艺（4 mil）
    "min_gap_mm": 0.127,            # 仅供记录：真正的间距检查在 DRC 阶段
    "min_hole_mm": 0.30,
    "min_hole_warn_mm": 0.20,
    "min_via_annular_mm": 0.10,     # 过孔环推荐值（低于它 = WARN，需与厂家确认）
    "min_via_annular_hard_mm": 0.05,  # 过孔环硬下限（低于它 = ERROR）
    "min_pth_annular_mm": 0.18,
    "min_board_mm": 5.0,
    "max_board_x_mm": 500.0,
    "max_board_y_mm": 500.0,
    "copper_to_edge_mm": 0.20,
    "outline_closure_tol_mm": 0.01,
    "outline_self_intersection": True,
    "bbox_slack_mm": 0.05,
    "max_layers": 6,
}

LEVEL_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


@dataclass
class Issue:
    level: str
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class PreflightReport:
    board: str
    rules: dict
    issues: list[Issue] = field(default_factory=list)
    gerbers: dict[str, gerber.GerberFile] = field(default_factory=dict)
    drills: dict[str, excellon.DrillFile] = field(default_factory=dict)
    layers_found: dict[str, str] = field(default_factory=dict)  # kicad 层名 -> 文件
    outline_box: tuple[float, float, float, float] | None = None
    # 解析器报告的"未实现语法"，按文件汇总：非空即阻断发布
    unsupported: dict[str, list[str]] = field(default_factory=dict)
    # 本模块实际执行了哪些检查：gate 用它判断覆盖率
    coverage: dict[str, str] = field(default_factory=dict)
    # 含 %LPC 等未建模语法、几何检查已降级的层（kicad 层名）
    degraded_layers: set[str] = field(default_factory=set)
    # 源设计信息（层数等），用于按工艺档位选阈值
    design: dict = field(default_factory=dict)

    # ---------------- 便捷 ----------------
    def add(self, level: str, code: str, message: str, **detail):
        self.issues.append(Issue(level, code, message, detail))

    def count(self, level: str) -> int:
        return sum(1 for i in self.issues if i.level == level)

    @property
    def ok(self) -> bool:
        """仅代表文件级预检通过；能否发布由 gate 决定。"""
        return self.count("ERROR") == 0 and not self.unsupported

    def sorted_issues(self):
        return sorted(self.issues, key=lambda i: (LEVEL_ORDER.get(i.level, 9), i.code))

    # ---------------- 输出 ----------------
    def composition_table(self) -> list[str]:
        rows = ["--- 文件组成 ---",
                f"  {'层':<10}{'文件':<40}{'孔径/图元':>12}  {'尺寸 mm':>16}"]
        for kicad in [l.kicad for l in layers.LAYERS if l.kicad in self.gerbers]:
            g = self.gerbers[kicad]
            b = g.bbox_centerline if kicad == "Edge.Cuts" else g.bbox
            size = f"{b[2] - b[0]:.3f}×{b[3] - b[1]:.3f}" if b else "-"
            rows.append(f"  {kicad:<10}{os.path.basename(g.path):<40}"
                        f"{len(g.apertures):>4}孔 {len(g.flashes):>5}闪 "
                        f"{len(g.strokes):>5}线 {len(g.regions):>3}区  {size:>16}")
        for name, d in self.drills.items():
            tools = " ".join(f"T{t.code}ø{t.diameter:.3f}×{t.hits}" for t in d.tools.values())
            slots = f" + {d.total_slots} 槽孔" if d.total_slots else ""
            rows.append(f"  {'钻孔':<10}{name:<40}{d.total_holes:>4}孔{slots:<8}   {tools}")
        rows.append("")
        return rows

    def to_text(self) -> str:
        out = [f"嘉立创下单预检报告 —— {self.board}", "=" * 72]
        n_e, n_w, n_i = self.count("ERROR"), self.count("WARN"), self.count("INFO")
        out.append(f"结论：{'通过' if self.ok else '不通过'}   "
                   f"ERROR {n_e} / WARN {n_w} / INFO {n_i}")
        if self.outline_box:
            x0, y0, x1, y1 = self.outline_box
            out.append(f"板框尺寸：{x1 - x0:.3f} × {y1 - y0:.3f} mm"
                       f"（x {x0:.3f}…{x1:.3f}, y {y0:.3f}…{y1:.3f}）")
        out.append("")
        out += self.composition_table()
        for lvl in ("ERROR", "WARN", "INFO"):
            rows = [i for i in self.sorted_issues() if i.level == lvl]
            if not rows:
                continue
            out.append(f"--- {lvl} ({len(rows)}) ---")
            for i in rows:
                out.append(f"  [{i.code}] {i.message}")
                if i.detail:
                    for k, v in i.detail.items():
                        out.append(f"        {k}: {v}")
            out.append("")
        if self.coverage:
            out.append("--- 检查覆盖 ---")
            for k, v in self.coverage.items():
                out.append(f"  {k:<22} {v}")
            out.append("")
        return "\n".join(out)

    def to_manifest(self) -> dict:
        return {
            "board": self.board,
            "ok": self.ok,
            "summary": {lv: self.count(lv) for lv in ("ERROR", "WARN", "INFO")},
            "outline_box_mm": self.outline_box,
            "layers": self.layers_found,
            "unsupported": self.unsupported,
            "coverage": self.coverage,
            "issues": [
                {"level": i.level, "code": i.code, "message": i.message, "detail": i.detail}
                for i in self.sorted_issues()
            ],
        }


def load_rules(path: str | None = None) -> dict:
    rules = dict(DEFAULT_RULES)
    if path and os.path.isfile(path):
        rules.update(json.load(open(path, "r", encoding="utf-8")))
    return rules


# ------------------------------------------------------------------ 几何工具


def _seg_intersect(a, b, c, d, tol=1e-9):
    """线段 ab 与 cd 是否"真交叉"（共享端点不算）。"""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    def on_seg(o, p, q):
        return (min(o[0], p[0]) - tol <= q[0] <= max(o[0], p[0]) + tol and
                min(o[1], p[1]) - tol <= q[1] <= max(o[1], p[1]) + tol)

    if (abs(a[0] - c[0]) < tol and abs(a[1] - c[1]) < tol) or \
       (abs(a[0] - d[0]) < tol and abs(a[1] - d[1]) < tol) or \
       (abs(b[0] - c[0]) < tol and abs(b[1] - c[1]) < tol) or \
       (abs(b[0] - d[0]) < tol and abs(b[1] - d[1]) < tol):
        return False            # 相邻段共享端点
    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > tol and d2 < -tol) or (d1 < -tol and d2 > tol)) and \
       ((d3 > tol and d4 < -tol) or (d3 < -tol and d4 > tol)):
        return True
    return False


def _cluster_endpoints(points, tol):
    """按真实欧氏距离把端点聚成顶点；返回 (顶点列表, 每个端点的顶点号)。"""
    verts: list[list[float]] = []
    idx_of: list[int] = []
    for (x, y) in points:
        for i, v in enumerate(verts):
            if math.hypot(x - v[0], y - v[1]) <= tol:
                v[0] = (v[0] * v[2] + x) / (v[2] + 1)
                v[1] = (v[1] * v[2] + y) / (v[2] + 1)
                v[2] += 1
                idx_of.append(i)
                break
        else:
            verts.append([x, y, 1])
            idx_of.append(len(verts) - 1)
    return verts, idx_of


# ------------------------------------------------------------------ 检查项


def _check_units(rep: PreflightReport, name: str, g: gerber.GerberFile):
    rep.coverage["单位"] = "已检查"
    if g.units != "mm":
        rep.add("ERROR", "UNIT-01", f"{name} 单位不是 mm（{g.units}）",
                file=name, units=g.units,
                note="英寸是合法 Gerber；本项目策略是统一 mm，避免与钻孔混淆")
    if g.fmt:
        dec = g.fmt[1]
        if dec not in (5, 6):
            rep.add("WARN", "FMT-01", f"{name} 坐标小数位为 {dec}，建议 5 或 6",
                    file=name, format=f"{g.fmt[0]}.{dec}")


def _check_min_feature(rep: PreflightReport, name: str, g: gerber.GerberFile,
                       ki: layers.LayerInfo):
    feats = []
    for ap in g.apertures.values():
        if ap.uses == 0:
            continue
        f = ap.min_feature()
        if f:
            feats.append((f, ap))
    if not feats:
        return
    rep.coverage["最小图形（孔径级）"] = "已检查"
    f, ap = min(feats, key=lambda t: t[0])
    comfortable = rep.rules["comfortable_trace_mm"]
    if int(rep.design.get("copper_layers") or 2) >= 4:
        comfortable = rep.rules.get("comfortable_trace_mm_multilayer", comfortable)
    if ki.kind != "copper":
        rep.add("INFO", "FEATURE-01",
                f"{name} 最小图形尺寸 {f:.4f} mm（D{ap.dcode} {ap.template}）", file=name)
        return
    rules = rep.rules
    limit = rules["min_trace_mm"]
    if f < limit - 1e-9:
        rep.add("ERROR", "COPPER-01",
                f"{name} 存在最小尺寸 {f:.4f} mm 的孔径（D{ap.dcode} {ap.template}），"
                f"低于阈值 {limit} mm", file=name, aperture=ap.dcode, value=round(f, 4))
    elif f < comfortable - 1e-9:
        rep.add("WARN", "COPPER-02",
                f"{name} 最小线宽 {f:.4f} mm 低于常规工艺 {comfortable} mm"
                f"（{rep.design.get('copper_layers', 2)} 层档）",
                file=name, value=round(f, 4))


def _check_outline(rep: PreflightReport, name: str, g: gerber.GerberFile):
    tol = rep.rules["outline_closure_tol_mm"]
    if not (g.strokes or g.regions):
        rep.add("ERROR", "OUTLINE-01", f"{name} 轮廓层为空")
        return

    # 1) 端点按真实距离聚类，统计每个顶点的度数
    pairs = [(st.points[0], st.points[-1]) for st in g.strokes if len(st.points) >= 2]
    verts, _ = _cluster_endpoints([p for pr in pairs for p in pr], tol)

    def vid(pt):
        best, bi = None, -1
        for i, v in enumerate(verts):
            d = math.hypot(pt[0] - v[0], pt[1] - v[1])
            if best is None or d < best:
                best, bi = d, i
        return bi

    deg = [0] * len(verts)
    parent = list(range(len(verts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a_pt, b_pt in pairs:
        a, b = vid(a_pt), vid(b_pt)
        deg[a] += 1
        deg[b] += 1
        parent[find(a)] = find(b)

    odd = [(round(verts[i][0], 3), round(verts[i][1], 3))
           for i, d in enumerate(deg) if d % 2 == 1]
    if odd:
        rep.add("ERROR", "OUTLINE-02",
                f"{name} 板框未闭合：{len(odd)} 个悬空端点（奇数度顶点）", points=odd[:8])
    else:
        rep.add("INFO", "OUTLINE-03", f"{name} 板框闭合性检查通过"
                f"（{len(pairs)} 段图形，{len(verts)} 个顶点）")

    # 2) 连通块数量（多块 = 有内部挖空或拼板，需要人工确认）
    roots = {find(i) for i in range(len(verts))}
    if len(roots) > 1:
        rep.add("WARN", "OUTLINE-06",
                f"{name} 有 {len(roots)} 个互不相连的轮廓（内部挖空/拼板/废边框？），"
                f"请确认嘉立创 CAM 的目标板划分与之一致")

    # 3) 自交检测
    if rep.rules.get("outline_self_intersection", True):
        segs = []
        for st in g.strokes:
            for p, q in zip(st.points, st.points[1:]):
                if math.hypot(q[0] - p[0], q[1] - p[1]) > 1e-6:
                    segs.append((p, q))
        hits = []
        for (s1, s2) in itertools.combinations(segs, 2):
            if (max(s1[0][0], s1[1][0]) < min(s2[0][0], s2[1][0]) or
                    max(s2[0][0], s2[1][0]) < min(s1[0][0], s1[1][0]) or
                    max(s1[0][1], s1[1][1]) < min(s2[0][1], s2[1][1]) or
                    max(s2[0][1], s2[1][1]) < min(s1[0][1], s1[1][1])):
                continue
            if _seg_intersect(s1[0], s1[1], s2[0], s2[1]):
                hits.append((tuple(round(v, 2) for v in s1[0]),
                             tuple(round(v, 2) for v in s1[1])))
                if len(hits) >= 5:
                    break
        if hits:
            rep.add("ERROR", "OUTLINE-05",
                    f"{name} 板框自交：{len(hits)} 处（蝴蝶结/重复描边会导致 CAM 解释成多块板）",
                    samples=hits[:5])
        rep.coverage["轮廓自交"] = "已检查"


def _check_bbox(rep: PreflightReport, name: str, g: gerber.GerberFile,
                outline: tuple[float, float, float, float] | None,
                ki: layers.LayerInfo):
    if outline is None:
        return
    b = g.bbox
    if b is None:
        return
    rep.coverage["层包络越界（粗检）"] = "已检查（bbox 级，非真实 DRC）"
    slack = rep.rules["bbox_slack_mm"]
    ox0, oy0, ox1, oy1 = outline
    over = max(ox0 - b[0], oy0 - b[1], b[2] - ox1, b[3] - oy1)
    if over > slack:
        rep.add("WARN", "BBOX-01",
                f"{name} 图形超出板框包络 {over:.3f} mm（粗检；请用 CAM/DRC 复核）",
                file=name, layer=ki.kicad,
                bbox=[round(v, 3) for v in b], outline=[round(v, 3) for v in outline])


def _check_drill(rep: PreflightReport, name: str, d: excellon.DrillFile):
    rules = rep.rules
    rep.coverage["钻孔"] = "已检查"
    if d.units != "mm":
        rep.add("ERROR", "DRILL-01", f"{name} 单位不是 mm（{d.units}）", file=name)
    md = d.min_diameter()
    if md is None:
        rep.add("ERROR", "DRILL-02", f"{name} 未找到刀具定义")
        return
    if md < rules["min_hole_warn_mm"] - 1e-9:
        rep.add("ERROR", "DRILL-03", f"{name} 最小孔径 {md:.3f} mm，低于 {rules['min_hole_warn_mm']} mm",
                file=name, value=round(md, 4))
    elif md < rules["min_hole_mm"] - 1e-9:
        rep.add("WARN", "DRILL-04", f"{name} 最小孔径 {md:.3f} mm 低于常规 {rules['min_hole_mm']} mm",
                file=name, value=round(md, 4))
    dups = d.duplicate_holes()
    if dups:
        rep.add("WARN", "DRILL-05", f"{name} 存在 {len(dups)} 个完全重合的孔",
                samples=[list(map(lambda v: round(v, 3) if isinstance(v, float) else v, k))
                         for k in dups[:5]])
    if d.total_slots:
        widths = sorted({d.tools[s[4]].diameter for s in d.slots if s[4] in d.tools})
        rep.add("INFO", "DRILL-08",
                f"{name} 含 {d.total_slots} 个 G85 槽孔（槽宽 {'/'.join(f'{w:g}' for w in widths)} mm）。"
                f"嘉立创支持 G85 槽孔，但需按槽孔工艺核对最小槽宽", file=name)
        if widths and widths[0] < rules["min_hole_mm"] - 1e-9:
            rep.add("WARN", "DRILL-09",
                    f"{name} 最小槽宽 {widths[0]:.3f} mm 低于常规 {rules['min_hole_mm']} mm", file=name)
    if d.plated is None:
        rep.add("WARN", "DRILL-06",
                f"{name} 无法判定 PTH/NPTH（建议文件名带 -PTH/-NPTH 或在头部写 TF.FileFunction）",
                file=name)
    if d.unsupported:
        for u in d.unsupported:
            rep.unsupported.setdefault(name, []).append(u)
        rep.add("ERROR", "PARSE-02",
                f"{name} 含未实现的钻孔语法：{'; '.join(sorted(set(d.unsupported)))}",
                file=name)


def _require_by_evidence(rep: PreflightReport):
    """必需层由"该层有没有东西"推断，而不是一张固定表（评审 S-06）。

    * 某面有焊盘（闪绘）却缺阻焊 → ERROR（该面必然要开窗）；
    * 某面有钢网却缺阻焊 → ERROR；
    * 铜层解析后为空 → ERROR（几乎总是导出漏层）；
    * 轮廓缺失单独由 OUTLINE-00 报。
    """
    def pads(side: str) -> int:
        k = f"{side}.Cu"
        g = rep.gerbers.get(k)
        return len(g.flashes) if g else 0

    for side, cu, mask, paste in (("F", "F.Cu", "F.Mask", "F.Paste"),
                                  ("B", "B.Cu", "B.Mask", "B.Paste")):
        if cu not in rep.gerbers:
            rep.add("ERROR", "LAYER-02", f"缺少 {cu} 层")
            continue
        n = pads(side)
        if n > 0 and mask not in rep.gerbers:
            rep.add("ERROR", "LAYER-04",
                    f"{cu} 有 {n} 个焊盘，但缺少 {mask}（该面必然需要阻焊开窗）")
        if paste in rep.gerbers and mask not in rep.gerbers:
            rep.add("ERROR", "LAYER-06", f"有 {paste} 却缺少 {mask}：钢网层无对应开窗")

    for kicad, g in rep.gerbers.items():
        ki = layers.layer_by_name(kicad)
        if not ki or ki.kind != "copper":
            continue
        if not (g.flashes or g.strokes or g.regions):
            rep.add("ERROR", "LAYER-05",
                    f"{kicad} 层解析后为空（导出漏层或文件名错配），不能投板")


def check(files: list[str], board: str = "", rules: dict | None = None,
          design: dict | None = None) -> PreflightReport:
    """文件级预检。

    design 可提供源设计信息（有则更严）：
      {"copper_layers": 4, "smt": True, "drill_expected": True}
    """
    rep = PreflightReport(board=board or _guess_board_name(files),
                          rules=rules or load_rules(),
                          design=dict(design or {}))
    drill_files: list[str] = []

    # 1) 分类
    for f in files:
        ki, why = layers.classify(f)
        if ki is None:
            ext_u = os.path.splitext(f)[1].upper()
            if ext_u in (".DRL", ".TXT", ".NCD"):
                drill_files.append(f)
                continue
            if f.lower().endswith(".json"):
                rep.add("INFO", "EXTRA-01", f"{os.path.basename(f)} 是附加文件（如飞针测试数据），嘉立创不解析")
                continue
            if ext_u in layers.OPTIONAL_SUFFIXES:
                rep.add("INFO", "EXTRA-02",
                        f"{os.path.basename(f)} 是可忽略的辅助层（{why}），嘉立创不解析")
                continue
            rep.add("WARN", "NAME-02", f"{os.path.basename(f)} 无法识别为制造层（{why}）")
            continue
        if ki.kicad in rep.layers_found:
            rep.add("ERROR", "DUP-01",
                    f"{os.path.basename(f)} 与 {os.path.basename(rep.layers_found[ki.kicad])} "
                    f"都指向 {ki.kicad} 层：打包会产生同名条目，必须只保留一个", file=f)
        rep.layers_found[ki.kicad] = f

    # 2) 解析 Gerber
    for kicad, f in rep.layers_found.items():
        ki = layers.layer_by_name(kicad)
        g = gerber.parse(f)
        rep.gerbers[kicad] = g
        if g.degraded:
            # 合法但本解析器未建模：不阻断，只降级本层的几何检查
            rep.add("WARN", "POLARITY-01",
                    f"{os.path.basename(f)} 含未建模的合法语法：{'; '.join(sorted(set(g.degraded)))}；"
                    f"该层几何检查降级（嘉立创可正常制板）",
                    file=os.path.basename(f))
            rep.degraded_layers.add(kicad)
        if g.unsupported:
            rep.unsupported.setdefault(os.path.basename(f), []).extend(g.unsupported)
            rep.add("ERROR", "PARSE-01",
                    f"{os.path.basename(f)} 含未实现的 Gerber 语法："
                    f"{'; '.join(sorted(set(g.unsupported)))}",
                    file=os.path.basename(f),
                    note="该层几何不可信，已阻断发布；请用 CAM 工具复核或扩展解析器")
        _check_units(rep, os.path.basename(f), g)
        if ki:
            _check_min_feature(rep, os.path.basename(f), g, ki)

    # 3) 板框
    outline_layer = rep.gerbers.get("Edge.Cuts")
    if outline_layer is None:
        rep.add("ERROR", "OUTLINE-00", "缺少板框层（Edge.Cuts → .GKO）")
    else:
        _check_outline(rep, os.path.basename(outline_layer.path), outline_layer)
        rep.outline_box = outline_layer.bbox_centerline
        if rep.outline_box:
            w = rep.outline_box[2] - rep.outline_box[0]
            h = rep.outline_box[3] - rep.outline_box[1]
            if w < rep.rules["min_board_mm"] or h < rep.rules["min_board_mm"]:
                rep.add("ERROR", "SIZE-01", f"板框尺寸 {w:.2f}×{h:.2f} mm 异常（过小）")
            if w > rep.rules["max_board_x_mm"] or h > rep.rules["max_board_y_mm"]:
                rep.add("WARN", "SIZE-02",
                        f"板框 {w:.1f}×{h:.1f} mm 超过单板 {rep.rules['max_board_x_mm']}"
                        f"×{rep.rules['max_board_y_mm']} mm 上限，需拼板/分单")

    # 4) 各层包络（粗检）
    for kicad, g in rep.gerbers.items():
        ki = layers.layer_by_name(kicad)
        if not ki or kicad == "Edge.Cuts" or ki.kind == "other":
            continue                      # 文档层不是制造层，越界无意义
        if kicad in rep.degraded_layers:
            rep.coverage[f"{kicad} 包络越界"] = "已跳过（该层含 %LPC 极性，未建模）"
            continue
        _check_bbox(rep, os.path.basename(g.path), g, rep.outline_box, ki)

    # 5) 钻孔（先按内容判定，再按文件名兜底）
    text_candidates = []
    for f in drill_files:
        d = excellon.parse(f)
        if not d.tools or (not d.holes and not d.slots):
            text_candidates.append(f)          # .txt 说明文件之类
            continue
        rep.drills[os.path.basename(f)] = d
        _check_drill(rep, os.path.basename(f), d)
    for f in text_candidates:
        rep.add("INFO", "EXTRA-03", f"{os.path.basename(f)} 不是钻孔文件（无刀具/孔），按文本附件忽略")
    if not rep.drills and rep.gerbers.get("F.Cu"):
        expectation = True if design is None else design.get("drill_expected", True)
        if expectation:
            rep.add("ERROR", "DRILL-00", "缺少钻孔文件（源设计未声明无孔）")

    # 6) 两个钻孔文件孔位重合：报告，不在这里删（删孔由 package 做严格判定）
    if len(rep.drills) >= 2:
        names = list(rep.drills)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = rep.drills[names[i]], rep.drills[names[j]]
                if not a.holes or not b.holes:
                    continue
                sa = {(round(x, 3), round(y, 3)) for (x, y, _t) in a.holes}
                sb = {(round(x, 3), round(y, 3)) for (x, y, _t) in b.holes}
                inter = sa & sb
                if len(inter) > 0.5 * min(len(sa), len(sb)):
                    rep.add("WARN", "DRILL-07",
                            f"{names[i]} 与 {names[j]} 孔位高度重合（{len(inter)} 个相同孔）："
                            f"只应提交一份，或确认是否为 PTH/NPTH 拆分。"
                            f"打包器仅在能证明整份子集时才自动省略",
                            files=[names[i], names[j]], common_holes=len(inter))

    # 7) 必需层（按证据推断）+ 内层连续性
    _require_by_evidence(rep)
    rep.coverage["必需层（按证据）"] = "已检查"
    inner = sorted(int(k[2:-3]) for k in rep.layers_found
                   if k.startswith("In") and k.endswith(".Cu"))
    if inner:
        missing = [n for n in range(1, max(inner) + 1) if n not in inner]
        if missing:
            rep.add("ERROR", "LAYER-03",
                    f"内层不连续：缺少 " + "、".join(f"In{n}.Cu" for n in missing)
                    + f"（现有 In{inner}）")
    if design and design.get("copper_layers"):
        want = int(design["copper_layers"])
        got = 2 + len(inner)
        if got != want:
            rep.add("ERROR", "LAYER-07",
                    f"导出铜层数 {got} 与源设计 {want} 不一致（漏内层是最常见的多层板事故）",
                    layers=sorted(rep.layers_found))

    # 8) 命名
    for f in files:
        b = os.path.basename(f)
        if os.path.splitext(b)[1].lower() in (".txt", ".json", ".md"):
            continue
        if any(ch in b for ch in " \t（）()#&+'\"，,，") or any(ord(ch) > 127 for ch in b):
            rep.add("WARN", "NAME-01",
                    f"文件名 {b} 含空格/中文/特殊字符，部分下单入口会解析失败", file=b)

    # 9) 覆盖登记：本模块没做的事要写清楚，别让"通过"被误读
    rep.coverage.setdefault("最小图形（孔径级）", "未执行（无可用孔径）")
    rep.coverage["异网间距/连通性"] = "未执行（需 DRC 阶段）"
    rep.coverage["铜到板边（真实距离）"] = "未执行（需 DRC 阶段）"
    rep.coverage["丝印压焊盘（几何）"] = "未执行（需 DRC/几何阶段）"
    return rep


def _guess_board_name(files: list[str]) -> str:
    for f in sorted(files):
        ki, _why = layers.classify(f)
        if ki is None:
            continue
        stem = os.path.splitext(os.path.basename(f))[0]
        stem = stem.replace("Gerber_", "")
        for tok in ("TopSilkscreenLayer", "BottomSilkscreenLayer",
                    "TopSolderMaskLayer", "BottomSolderMaskLayer",
                    "TopPasteMaskLayer", "BottomPasteMaskLayer",
                    "BoardOutlineLayer", "DocumentLayer", "DrillDrawingLayer",
                    "TopLayer", "BottomLayer",
                    "_TopSilkscreenLayer", "_BottomSilkscreenLayer",
                    "_TopSolderMaskLayer", "_BottomSolderMaskLayer",
                    "_TopPasteMaskLayer", "_BottomPasteMaskLayer",
                    "_BoardOutlineLayer", "_DocumentLayer",
                    "_TopLayer", "_BottomLayer",
                    "-F_Cu", "-B_Cu", "-Edge_Cuts", "-F_Mask", "-B_Mask",
                    "-F_SilkS", "-B_SilkS", "-F_Paste", "-B_Paste"):
            stem = stem.replace(tok, "")
        stem = re.sub(r"[-_]+$", "", stem).strip()
        if stem:
            return stem
    return "PCB"
