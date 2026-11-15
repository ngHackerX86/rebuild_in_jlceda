"""板级几何检查：从 KiCad 的板子模型直接读数据，比解析 Gerber 更准。

文件级预检查不出这些：过孔环、焊盘环宽、孔到孔间距、丝印压焊盘、
铜到板边距离。KiCad 提供了现成的对象模型，直接算即可。
"""
from __future__ import annotations

import math
from collections import defaultdict

from .preflight import Issue


def _mm(v) -> float:
    return v / 1_000_000.0     # pcbnew 内部单位是 nm


def check_board(board, rules: dict) -> list[Issue]:
    issues: list[Issue] = []
    try:
        import pcbnew
    except Exception:
        return [Issue("INFO", "BOARD-00", "pcbnew 不可用，跳过板级检查")]

    # ---------- 走线宽度 ----------
    min_track = None
    min_track_item = None
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            continue
        w = _mm(t.GetWidth())
        if min_track is None or w < min_track:
            min_track, min_track_item = w, t
    if min_track is not None:
        if min_track < rules["min_trace_mm"] - 1e-9:
            issues.append(Issue("ERROR", "TRACK-01",
                                f"最小走线宽度 {min_track:.4f} mm 低于阈值 {rules['min_trace_mm']} mm",
                                {"x": round(_mm(min_track_item.GetStart().x), 3),
                                 "y": round(_mm(min_track_item.GetStart().y), 3)}))
        elif min_track < rules["comfortable_trace_mm"] - 1e-9:
            issues.append(Issue("WARN", "TRACK-02",
                                f"最小走线宽度 {min_track:.4f} mm 低于常规免费工艺"))

    # ---------- 过孔 ----------
    min_via_drill = None
    min_ring = None
    ring_where = ""
    ring_pair = (0.0, 0.0)
    for t in board.GetTracks():
        if not isinstance(t, pcbnew.PCB_VIA):
            continue
        d = _mm(t.GetDrillValue())
        try:
            w = _mm(t.GetWidth(t.TopLayer()))      # KiCad 10 要求层参数
        except TypeError:
            w = _mm(t.GetWidth())
        ring = (w - d) / 2
        if min_ring is None or ring < min_ring:
            min_ring, ring_pair = ring, (d, w)
            pos = t.GetPosition()
            ring_where = f"({_mm(pos.x):.2f}, {_mm(pos.y):.2f})"
        min_via_drill = d if min_via_drill is None else min(min_via_drill, d)
    if min_via_drill is not None:
        if min_via_drill < rules["min_hole_mm"] - 1e-9:
            issues.append(Issue("WARN" if min_via_drill >= rules["min_hole_warn_mm"] else "ERROR",
                                "VIA-01", f"最小过孔孔径 {min_via_drill:.3f} mm"))
        soft = rules.get("min_via_annular_mm", 0.10)
        hard = rules.get("min_via_annular_hard_mm", 0.05)
        detail = {"ring": round(min_ring, 4), "drill": ring_pair[0], "pad": ring_pair[1],
                  "at": ring_where}
        if min_ring < hard - 1e-9:
            issues.append(Issue("ERROR", "VIA-02",
                                f"最小过孔环宽 {min_ring:.4f} mm 低于硬下限 {hard} mm"
                                f"（ø{ring_pair[0]:.3f} 孔 / ø{ring_pair[1]:.3f} 盘，{ring_where}）",
                                detail))
        elif min_ring < soft - 1e-9:
            issues.append(Issue("WARN", "VIA-02",
                                f"最小过孔环宽 {min_ring:.4f} mm 低于推荐值 {soft} mm"
                                f"（ø{ring_pair[0]:.3f} 孔 / ø{ring_pair[1]:.3f} 盘，{ring_where}）；"
                                f"需按厂家过孔能力确认", detail))

    # ---------- 焊盘 ----------
    min_pad_ring = None
    min_pad_ring_where = ""
    min_pad_drill = None
    min_pad_drill_where = ""
    smd = 0
    tht = 0
    npth = 0
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            ds = pad.GetDrillSize()
            # NPTH 安装孔本来就没有铜环，不能按"焊盘环宽"判
            is_npth = False
            try:
                is_npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
            except Exception:
                pass
            if (ds.x > 0 or ds.y > 0) and is_npth:
                npth += 1
            if (ds.x > 0 or ds.y > 0) and not is_npth:
                tht += 1
                d = _mm(min(ds.x, ds.y))
                where = f"{fp.GetReference()}.{pad.GetNumber()}"
                if min_pad_drill is None or d < min_pad_drill:
                    min_pad_drill, min_pad_drill_where = d, where
                sz = pad.GetSize()
                ring = (_mm(min(sz.x, sz.y)) - d) / 2
                if min_pad_ring is None or ring < min_pad_ring:
                    min_pad_ring, min_pad_ring_where = ring, where
            else:
                smd += 1
    # 环宽恒为 0 的镀通孔（尺寸==钻孔）属于"无铜环的机械孔"，不是环宽不足
    if min_pad_ring is not None and min_pad_ring <= 1e-6:
        issues.append(Issue("INFO", "PAD-03",
                            f"存在焊盘尺寸=钻孔的镀通孔（{min_pad_ring_where}）："
                            f"无铜环，按机械镀通孔处理（常见于电池座/固定孔）",
                            {"pad": min_pad_ring_where}))
        min_pad_ring = None
    for label, val, code in (("焊盘钻孔", min_pad_drill, "PAD-01"),
                             ("焊盘环宽", min_pad_ring, "PAD-02")):
        if val is None:
            continue
        limit = (rules["min_hole_mm"] if code == "PAD-01"
                 else rules.get("min_pth_annular_mm", 0.18))
        if val < limit - 1e-9:
            where = min_pad_drill_where if code == "PAD-01" else min_pad_ring_where
            issues.append(Issue("WARN" if code == "PAD-01" else "ERROR", code,
                                f"最小{label} {val:.4f} mm 低于 {limit} mm（{where}）",
                                {"pad": where, "value": round(val, 4), "limit": limit}))

    # ---------- 孔到孔间距 ----------
    holes: list[tuple[float, float, float]] = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            ds = pad.GetDrillSize()
            if ds.x > 0 or ds.y > 0:
                p = pad.GetPosition()
                holes.append((_mm(p.x), _mm(p.y), _mm(min(ds.x, ds.y))))
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            p = t.GetPosition()
            holes.append((_mm(p.x), _mm(p.y), _mm(t.GetDrillValue())))
    cell = 1.0
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x, y, _d) in enumerate(holes):
        grid[(int(x // cell), int(y // cell))].append(i)
    worst = None
    for i, (x, y, d) in enumerate(holes):
        cx, cy = int(x // cell), int(y // cell)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in grid.get((gx, gy), ()):
                    if j <= i:
                        continue
                    x2, y2, d2 = holes[j]
                    edge_gap = math.hypot(x - x2, y - y2) - (d + d2) / 2
                    if worst is None or edge_gap < worst[0]:
                        worst = (edge_gap, x, y, x2, y2)
    if worst and worst[0] < rules.get("min_hole_to_hole_mm", 0.25) - 1e-9:
        issues.append(Issue("WARN", "HOLE-01",
                            f"最小孔边间距 {worst[0]:.3f} mm（阈值 0.25 mm）",
                            {"p1": [round(worst[1], 3), round(worst[2], 3)],
                             "p2": [round(worst[3], 3), round(worst[4], 3)]}))

    # ---------- 丝印压焊盘（用 bbox 粗判，避免引入几何库）----------
    silk_pads = 0
    try:
        mask_pads = [p for fp in board.GetFootprints() for p in fp.Pads()]
        for item in board.GetDrawings():
            if item.GetLayer() not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                continue
            bb = item.GetBoundingBox()
            layer = item.GetLayer()
            same_side = (pcbnew.F_SilkS if layer == pcbnew.F_SilkS else pcbnew.B_SilkS)
            for pad in mask_pads:
                pad_layer = pad.GetLayer()
                if pad_layer not in (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.F_Mask, pcbnew.B_Mask):
                    continue
                # 只和"同一面"的焊盘比较，避免跨面误判
                if (same_side == pcbnew.F_SilkS) != (pad_layer in (pcbnew.F_Cu, pcbnew.F_Mask)):
                    continue
                if pad.GetBoundingBox().Intersects(bb):
                    silk_pads += 1
                    break
    except Exception:
        pass
    if silk_pads:
        issues.append(Issue("INFO", "SILK-01",
                            f"约 {silk_pads} 个丝印图元与焊盘外框相交（嘉立创会削掉压焊盘部分，建议开启"
                            f"“从丝印中减去阻焊”或调整丝印）"))

    # ---------- 板层数 / 统计 ----------
    copper = board.GetCopperLayerCount()
    if copper > rules.get("max_layers", 6):
        issues.append(Issue("WARN", "STACK-01", f"{copper} 层板超出常规工艺（默认 ≤6 层）"))
    issues.append(Issue("INFO", "COVERAGE-01",
                        "本阶段未实现：异网铜间距、网络连通性、铜到板边的真实距离、"
                        "丝印压焊盘精确几何（这些依赖 DRC 阶段与 CAM 复核）"))
    issues.append(Issue("INFO", "STATS-01",
                        f"{copper} 层板；贴片焊盘 {smd}，镀通孔焊盘 {tht}，"
                        f"非镀通孔 {npth}，过孔/孔位合计 {len(holes)}"))
    return issues
