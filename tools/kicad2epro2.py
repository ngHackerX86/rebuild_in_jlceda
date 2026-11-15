# -*- coding: utf-8 -*-
"""KiCad 板 → 嘉立创EDA 专业版 3.x 工程（.epro2）。

格式依据真机工程 `ProPrj_USC_B_CTRL_P04_2026-11-14.epro2` 反推：

    zip = project2.json + IMAGE/ + <工程名>.epru
    .epru 每行一条记录： {"type":X,"ticket":N,"id":ID}||<payload>|
      * id 为字符串；复合键是 JSON 串： "[\"LAYER\",1]" / "[\"NET\",\"GND\"]"
      * payload 为字段表；几何单位 mil，角度为度
      * 文档以 DOCHEAD 开头：docType ∈ BOARD / PCB / FOOTPRINT / SCH / SCH_PAGE …
      * 每类元素前有 ELE_PLACEHOLDER{dataType,max} 声明 id 范围
      * FOOTPRINT 文档自带 LAYER/ACTIVE_LAYER/CANVAS/NET/META/ATTR，坐标相对器件原点
      * 点串语法：`[x0,y0,"L",x1,y1,x2,y2,…]`——“L” 只出现一次

用法：
    python tools/kicad2epro2.py board.kicad_pcb -o out/epro2 --name MyBoard
    python tools/kicad2epro2.py board.kicad_pcb --with-components --with-zones
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
import zipfile

MIL = 39.37007874015748
VERSION = "3.2.149.88089769"

LAYER_TABLE = [
    (1, "TOP", "Top Layer"), (2, "BOTTOM", "Bottom Layer"),
    (3, "TOP_SILK", "Top Silkscreen Layer"), (4, "BOT_SILK", "Bottom Silkscreen Layer"),
    (5, "TOP_SOLDER_MASK", "Top Solder Mask Layer"),
    (6, "BOT_SOLDER_MASK", "Bottom Solder Mask Layer"),
    (7, "TOP_PASTE_MASK", "Top Paste Mask Layer"),
    (8, "BOT_PASTE_MASK", "Bottom Paste Mask Layer"),
    (9, "TOP_ASSEMBLY", "Top Assembly Layer"),
    (10, "BOT_ASSEMBLY", "Bottom Assembly Layer"),
    (11, "OUTLINE", "Board Outline Layer"), (12, "MULTI", "Multi-Layer"),
    (13, "DOCUMENT", "Document Layer"), (14, "MECHANICAL", "Mechanical Layer"),
    (47, "HOLE", "Hole Layer"), (48, "COMPONENT_SHAPE", "Component Shape Layer"),
    (49, "COMPONENT_MARKING", "Component Marking Layer"),
    (50, "PIN_SOLDERING", "Pin Soldering Layer"),
    (51, "PIN_FLOATING", "Pin Floating Layer"),
    (52, "COMPONENT_MODEL", "Component Model Layer"),
    (56, "DRILL_DRAWING", "Drill Drawing Layer"), (57, "OTHER", "Ratline Layer"),
]

COLORS = {1: "#ff0000", 2: "#0000ff", 3: "#ffcc00", 4: "#66cc33", 5: "#800080",
          6: "#aa00ff", 7: "#808080", 8: "#800000", 9: "#33cc99", 10: "#5555ff",
          11: "#ff00ff", 12: "#c0c0c0", 13: "#ffffff", 14: "#f022f0", 47: "#222222",
          48: "#00cccc", 49: "#66ffcc", 50: "#cc9999", 51: "#ff99ff", 56: "#008080",
          57: "#6464ff"}


def mil(v_mm: float) -> float:
    return round(v_mm * MIL, 4)


class Log:
    """一条记录一行：HEAD||PAYLOAD|\\n"""

    def __init__(self):
        self.lines: list[str] = []
        self.ticket = 0

    def _t(self) -> int:
        self.ticket += 1
        return self.ticket

    @staticmethod
    def _fmt(head: dict, payload: dict) -> str:
        return (json.dumps(head, ensure_ascii=False, separators=(",", ":"))
                + "||"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "|\n")

    def dochead(self, doc_type: str, doc_uuid: str) -> None:
        head = {"type": "DOCHEAD", "ticket": self._t()}
        self.lines.append(self._fmt(head, {
            "docType": doc_type, "client": uuid.uuid4().hex[:16], "uuid": doc_uuid,
            "updateTime": int(time.time() * 1000),
            "version": str(int(time.time() * 1000)),
            "editVersion": VERSION, "user": {"uuid": uuid.uuid4().hex[:16]}}))

    def rec(self, typ: str, rid, payload: dict, first: bool = True) -> None:
        head = {"type": typ, "ticket": self._t()}
        head["id"] = (json.dumps(list(rid), ensure_ascii=False, separators=(",", ":"))
                      if isinstance(rid, (list, tuple)) else rid)
        if first:
            head["firstTicket"] = self._t()
        self.lines.append(self._fmt(head, payload))

    def text(self) -> str:
        return "".join(self.lines)


def emit_layers(log: Log, used: set[int]) -> None:
    table = list(LAYER_TABLE) + [(i, "SIGNAL", f"Inner{i - 14}") for i in range(15, 47)]
    for lid, ltype, lname in table:
        log.rec("LAYER", ["LAYER", lid],
                {"layerId": lid, "layerType": ltype, "layerName": lname,
                 "use": lid in used, "show": lid in used, "locked": False,
                 "activeColor": COLORS.get(lid, "#00ff00"), "activateTransparency": 1,
                 "inactiveColor": "#7f0000", "inactiveTransparency": 0.5})
    log.rec("ACTIVE_LAYER", "ACTIVE_LAYER", {"layerId": 1})
    log.rec("CANVAS", "CANVAS",
            {"originX": 0, "originY": 0, "unit": "mm", "gridXSize": 10, "gridYSize": 10,
             "snapXSize": 0.5, "snapYSize": 0.5, "gridType": "NONE",
             "multiGridType": "NONE", "highlightValue": 0.5})


def emit_placeholders(log: Log, counts: dict[str, int]) -> None:
    for i, (kind, n) in enumerate(counts.items(), 1):
        log.rec("ELE_PLACEHOLDER", f"placeholder{i}",
                {"dataType": kind, "max": max(int(n), 1)})


# KiCad 的层 ID 是固定的（改名不改 ID），所以映射应该按 ID 而不是显示名。
# EDA 侧：1/2 顶底铜，3/4 顶底丝印，5/6 顶底阻焊，7/8 顶底钢网，11 板框，13 文档层，15+ 内层
def _fixed_layer_map() -> dict[int, int]:
    """固定层映射。**必须用 pcbnew 常量**：KiCad 10 起层 ID 与旧版不同
    （实测 B_Cu=2、In1_Cu=4、In2_Cu=6），写死数字会全部错位。"""
    try:
        import pcbnew as _p
    except Exception:
        return {}
    return {int(_p.F_Cu): 1, int(_p.B_Cu): 2,
            int(_p.F_SilkS): 3, int(_p.B_SilkS): 4,
            int(_p.F_Mask): 5, int(_p.B_Mask): 6,
            int(_p.F_Paste): 7, int(_p.B_Paste): 8,
            int(_p.Edge_Cuts): 11}


class LayerMapper:
    """把 KiCad 层 ID 映射到 EDA 层 ID；记录无法映射的层。

    内层按"启用叠层顺序"映射（In1→15、In2→16…），不再依赖 `In1.Cu` 这种显示名，
    所以把 In1 改名成 PWR 也不会掉到文档层。
    """

    def __init__(self, board, allow_unknown: bool = False):
        self.board = board
        self.allow_unknown = allow_unknown
        self.by_id = _fixed_layer_map()
        self.unknown: dict[str, int] = {}
        self._fill_inner()

    def _fill_inner(self):
        try:
            stack = list(self.board.GetEnabledLayers().CuStack())
        except Exception:
            stack = []
        try:
            import pcbnew as _p
            ends = {int(_p.F_Cu), int(_p.B_Cu)}
        except Exception:
            ends = {0, 2}
        inner = [lid for lid in stack if int(lid) not in ends]
        for idx, lid in enumerate(inner, start=1):
            self.by_id[int(lid)] = 14 + idx

    def name(self, layer_id: int) -> str:
        try:
            return self.board.GetLayerName(int(layer_id))
        except Exception:
            return str(layer_id)

    def get(self, layer_id: int) -> int:
        lid = int(layer_id)
        if lid in self.by_id:
            return self.by_id[lid]
        key = self.name(lid)
        self.unknown[key] = self.unknown.get(key, 0) + 1
        return 13                      # 文档层（兜底，最后由 build() 决定是否阻断）

    def problems(self) -> list[str]:
        return [f"{k}（{v} 个对象）" for k, v in sorted(self.unknown.items())]


def layer_id(kicad_name: str) -> int:
    fixed = {"F.Cu": 1, "B.Cu": 2, "F.SilkS": 3, "F.Silkscreen": 3,
             "B.SilkS": 4, "B.Silkscreen": 4, "F.Mask": 5, "B.Mask": 6,
             "F.Paste": 7, "B.Paste": 8, "Edge.Cuts": 11}
    if kicad_name in fixed:
        return fixed[kicad_name]
    if kicad_name.startswith("In") and kicad_name.endswith(".Cu"):
        return 14 + int(kicad_name[2:-3])
    return 13


def _poly_path(points, closed: bool = False) -> list:
    """EasyEDA 点串：只写一次 "L"，后面平铺坐标。"""
    if not points:
        return []
    path = [round(points[0][0], 4), round(points[0][1], 4), "L"]
    for x, y in points[1:]:
        path += [round(x, 4), round(y, 4)]
    if closed:
        path += [round(points[0][0], 4), round(points[0][1], 4)]
    return path


def _rdp(points, tol):
    """Douglas-Peucker 简化（闭合环按首点切分处理）。"""
    if len(points) < 4:
        return points

    def seg_dist(pt, a, b):
        ax, ay = a
        bx, by = b
        px, py = pt
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def simplify(pts):
        if len(pts) < 3:
            return pts
        dmax, idx = 0.0, 0
        for i in range(1, len(pts) - 1):
            d = seg_dist(pts[i], pts[0], pts[-1])
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol:
            return (simplify(pts[:idx + 1])[:-1] + simplify(pts[idx:]))
        return [pts[0], pts[-1]]

    return simplify(points)


def _chain_edges(edges, tol_nm: int = 10000):
    """把线段/弧段串成闭合环。

    端点匹配用**容差聚类**而不是"量化到 1 µm 后严格相等"：
    实测 KiCad 9 板的板框圆弧与直边接点会差几微米（C3 板是 4.5 µm），
    严格相等的写法会串不成环，导致整块板框丢失。
    默认容差 10 µm，可在调用处覆盖。
    """
    import pcbnew

    nodes: list[list[int]] = []          # 每个节点的坐标 [x, y]
    node_of: list[int] = []              # 每个端点属于哪个节点

    def node_id(pt) -> int:
        for idx, (nx, ny) in enumerate(nodes):
            if abs(pt.x - nx) <= tol_nm and abs(pt.y - ny) <= tol_nm:
                return idx
        nodes.append([pt.x, pt.y])
        return len(nodes) - 1

    for a, b in edges:
        node_of.append(node_id(a))
        node_of.append(node_id(b))

    # 邻接表：节点 → [(另一端节点, 边序号)]
    adj: dict[int, list[tuple[int, int]]] = {}
    for i, (a, b) in enumerate(edges):
        na, nb = node_of[2 * i], node_of[2 * i + 1]
        if na == nb:                      # 退化边，跳过
            continue
        adj.setdefault(na, []).append((nb, i))
        adj.setdefault(nb, []).append((na, i))

    loops, left = [], set(range(len(edges)))
    while left:
        e0 = next(iter(left))
        left.discard(e0)
        na, nb = node_of[2 * e0], node_of[2 * e0 + 1]
        loop_nodes = [na, nb]
        loop_edges = [e0]
        while True:
            tail = loop_nodes[-1]
            nxt = None
            for (other, idx) in adj.get(tail, []):
                if idx in left:
                    nxt = (other, idx)
                    break
            if nxt is None:
                break
            left.discard(nxt[1])
            loop_nodes.append(nxt[0])
            loop_edges.append(nxt[1])
            if nxt[0] == loop_nodes[0]:
                break
        closed = len(loop_nodes) > 2 and loop_nodes[-1] == loop_nodes[0]
        if closed and len(loop_nodes) >= 3:
            loops.append([pcbnew.VECTOR2I(nodes[n][0], nodes[n][1]) for n in loop_nodes])

    if loops:
        return loops

    # 没串成环也要给个交代：把最长的一条链首尾相接，作为板框兜底
    chains: list[list[int]] = []
    seen: set[int] = set()
    for start in range(len(edges)):
        if start in seen:
            continue
        na, nb = node_of[2 * start], node_of[2 * start + 1]
        chain = [na, nb]
        seen.add(start)
        while True:
            tail = chain[-1]
            nxt = None
            for (other, idx) in adj.get(tail, []):
                if idx not in seen:
                    nxt = (other, idx)
                    break
            if nxt is None:
                break
            seen.add(nxt[1])
            chain.append(nxt[0])
        if len(chain) >= 3:
            chains.append(chain)
    if not chains:
        return []
    longest = max(chains, key=len)
    return [[pcbnew.VECTOR2I(nodes[n][0], nodes[n][1]) for n in longest]]


def build(board_path: str, out_dir: str, name: str, with_components: bool = False,
          with_zones: bool = False, board_pads: bool = True,
          zone_tol: float = 0.0, zone_net: bool = True,
          placeholders: bool = True, only: str | None = None,
          pad_subset: str = "all", pad_type: str = "auto") -> str:
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    log = Log()

    # ============ BOARD 文档 ============
    board_uuid = uuid.uuid4().hex[:16]
    log.dochead("BOARD", board_uuid)
    log.rec("META", "META", {"title": name, "zIndex": 3})
    log.rec("META_MODIFY", "META_MODIFY", {"updateTime": int(time.time() * 1000)})

    # ============ PCB 文档 ============
    log.dochead("PCB", uuid.uuid4().hex[:16])
    log.rec("META", "META", {"title": name, "parent": "", "source": "",
                             "board": board_uuid, "zIndex": None})
    copper = int(board.GetCopperLayerCount())
    used = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12}
    for i in range(1, copper - 1):
        used.add(14 + i)
    emit_layers(log, used)
    for lid in sorted(used):
        log.rec("LAYER_PHYS", ["LAYER_PHYS", lid],
                {"material": None, "thickness": 0, "permittivity": None,
                 "lossTangent": None, "isKeepIsland": True, "zIndex": 1})
    log.rec("PRIMITIVE", ["PRIMITIVE", "ALL"], {"display": True, "pick": False})
    nets = [""]
    for _code, net in board.GetNetInfo().NetsByNetcode().items():
        n = net.GetNetname()
        if n and n not in nets:
            nets.append(n)
    for n in nets:
        log.rec("NET", ["NET", n],
                {"netType": None, "specialColor": None, "retLine": True,
                 "differentialName": None, "isPositiveNet": False,
                 "equalLengthGroupName": None})

    # 板框：Edge.Cuts → 闭合 POLY（层 11）
    edges = []
    for d in board.GetDrawings():
        if d.GetLayer() != pcbnew.Edge_Cuts:
            continue
        shape = d.GetShape()
        if shape == pcbnew.SHAPE_T_SEGMENT:
            edges.append((d.GetStart(), d.GetEnd()))
        elif shape == pcbnew.SHAPE_T_ARC:
            c = d.GetCenter()
            r = math.hypot(d.GetStart().x - c.x, d.GetStart().y - c.y)
            a0 = math.atan2(d.GetStart().y - c.y, d.GetStart().x - c.x)
            a1 = math.atan2(d.GetEnd().y - c.y, d.GetEnd().x - c.x)
            if a1 <= a0:
                a1 += 2 * math.pi
            for i in range(16):
                t0 = a0 + (a1 - a0) * i / 16
                t1 = a0 + (a1 - a0) * (i + 1) / 16
                edges.append((pcbnew.VECTOR2I(int(c.x + r * math.cos(t0)),
                                              int(c.y + r * math.sin(t0))),
                              pcbnew.VECTOR2I(int(c.x + r * math.cos(t1)),
                                              int(c.y + r * math.sin(t1)))))
        elif shape == pcbnew.SHAPE_T_RECT:
            p = [d.GetStart(), pcbnew.VECTOR2I(d.GetEnd().x, d.GetStart().y),
                 d.GetEnd(), pcbnew.VECTOR2I(d.GetStart().x, d.GetEnd().y)]
            for i in range(4):
                edges.append((p[i], p[(i + 1) % 4]))

    n_poly = 0
    for loop in _chain_edges(edges):
        pts = [(mil(p.x / 1e6), -mil(p.y / 1e6)) for p in loop]
        n_poly += 1
        log.rec("POLY", f"outline{n_poly}", {
            "partitionId": "", "groupId": 0, "netName": "", "layerId": 11,
            "width": mil(0.1), "path": _poly_path(pts, closed=True),
            "locked": False, "zIndex": 4, "polyType": "NORMAL"})

    # 板级丝印
    n_silk = 0
    for d in board.GetDrawings():
        if d.GetLayer() not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            continue
        lid = 3 if d.GetLayer() == pcbnew.F_SilkS else 4
        width = mil(d.GetWidth() / 1e6) if hasattr(d, "GetWidth") else mil(0.12)
        try:
            shape = d.GetShape()
        except Exception:
            continue
        pts = []
        if shape == pcbnew.SHAPE_T_SEGMENT:
            pts = [(mil(d.GetStart().x / 1e6), -mil(d.GetStart().y / 1e6)),
                   (mil(d.GetEnd().x / 1e6), -mil(d.GetEnd().y / 1e6))]
        elif shape == pcbnew.SHAPE_T_ARC:
            c = d.GetCenter()
            r = math.hypot(d.GetStart().x - c.x, d.GetStart().y - c.y)
            a0 = math.atan2(d.GetStart().y - c.y, d.GetStart().x - c.x)
            a1 = math.atan2(d.GetEnd().y - c.y, d.GetEnd().x - c.x)
            if a1 <= a0:
                a1 += 2 * math.pi
            pts = [(mil((c.x + r * math.cos(a0 + (a1 - a0) * k / 16)) / 1e6),
                    -mil((c.y + r * math.sin(a0 + (a1 - a0) * k / 16)) / 1e6))
                   for k in range(17)]
        elif shape == pcbnew.SHAPE_T_RECT:
            a, b = d.GetStart(), d.GetEnd()
            corners = [a, pcbnew.VECTOR2I(b.x, a.y), b, pcbnew.VECTOR2I(a.x, b.y)]
            pts = [(mil(c.x / 1e6), -mil(c.y / 1e6)) for c in corners]
        elif shape == pcbnew.SHAPE_T_CIRCLE:
            c, r = d.GetCenter(), d.GetRadius() / 1e6
            pts = [(mil(c.x / 1e6 + r * math.cos(2 * math.pi * k / 24)),
                    -mil(c.y / 1e6 + r * math.sin(2 * math.pi * k / 24)))
                   for k in range(24)]
        if len(pts) < 2:
            continue
        n_silk += 1
        log.rec("POLY", f"silk{n_silk}", {
            "partitionId": "", "groupId": 0, "netName": "", "layerId": lid,
            "width": width,
            "path": _poly_path(pts, closed=(shape == pcbnew.SHAPE_T_RECT)),
            "locked": False, "zIndex": 4, "polyType": "NORMAL"})

    # 走线 / 过孔
    n_line = n_via = 0
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            pos = t.GetPosition()
            n_via += 1
            log.rec("VIA", f"via{n_via}", {
                "partitionId": "", "groupId": 0, "netName": t.GetNetname(),
                "ruleName": "", "centerX": mil(pos.x / 1e6),
                "centerY": -mil(pos.y / 1e6),
                "holeDiameter": mil(t.GetDrillValue() / 1e6),
                "viaDiameter": mil(t.GetWidth(t.TopLayer()) / 1e6),
                "viaType": "NORMAL", "topSolderExpansion": None,
                "bottomSolderExpansion": None, "locked": False,
                "unusedInnerLayers": []})
            continue
        s, e = t.GetStart(), t.GetEnd()
        lid = layer_id(board.GetLayerName(t.GetLayer()))
        segs = [(s, e)]
        if isinstance(t, pcbnew.PCB_ARC):
            c = t.GetCenter()
            r = math.hypot(s.x - c.x, s.y - c.y)
            a0 = math.atan2(s.y - c.y, s.x - c.x)
            a1 = math.atan2(e.y - c.y, e.x - c.x)
            if a1 <= a0:
                a1 += 2 * math.pi
            segs, prev = [], s
            for i in range(1, 17):
                th = a0 + (a1 - a0) * i / 16
                nxt = pcbnew.VECTOR2I(int(c.x + r * math.cos(th)),
                                      int(c.y + r * math.sin(th)))
                segs.append((prev, nxt))
                prev = nxt
        for a, b in segs:
            n_line += 1
            log.rec("LINE", f"line{n_line}", {
                "partitionId": "", "groupId": "1", "netName": t.GetNetname(),
                "layerId": lid,
                "startX": mil(a.x / 1e6), "startY": -mil(a.y / 1e6),
                "endX": mil(b.x / 1e6), "endY": -mil(b.y / 1e6),
                "width": mil(t.GetWidth() / 1e6), "locked": False, "zIndex": -1})

    # 铺铜 → FILL（实心区域）
    n_fill = 0
    if with_zones:
        for z in board.Zones():
            for lid_k in z.GetLayerSet().Seq():
                if not pcbnew.IsCopperLayer(lid_k):
                    continue
                poly = z.GetFilledPolysList(lid_k)
                for oi in range(poly.OutlineCount()):
                    chain = poly.Outline(oi)
                    pts = [(mil(chain.CPoint(i).x / 1e6), -mil(chain.CPoint(i).y / 1e6))
                           for i in range(chain.PointCount())]
                    if len(pts) < 3:
                        continue
                    if zone_tol > 0 and len(pts) > 4:
                        pts = _rdp(pts, zone_tol)
                    n_fill += 1
                    log.rec("FILL", f"fill{n_fill}", {
                        "partitionId": "", "groupId": 0,
                        "netName": z.GetNetname() if zone_net else "",
                        "layerId": layer_id(board.GetLayerName(lid_k)),
                        "width": 0.2, "fillStyle": "SOLID",
                        "path": [_poly_path(pts, closed=True)],
                        "locked": False, "zIndex": -1, "isBridgingCopper": False,
                        "networkList": [], "refs": []})

    # ============ 板上直接放焊盘/丝印/位号（自定义封装路线）============
    board_pad = board_silk = board_text = 0
    if board_pads:
        for fp in board.GetFootprints():
            origin = fp.GetPosition()
            ang = fp.GetOrientationDegrees()

            def abs_pt(pt):
                return (mil(pt.x / 1e6), -mil(pt.y / 1e6))

            for pad in (fp.Pads() if only in (None, "pads") else []):
                size, drill = pad.GetSize(), pad.GetDrillSize()
                plated_attr = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                if pad_subset == "smd" and plated_attr:
                    continue
                if pad_subset == "tht" and not plated_attr:
                    continue
                shape = pad.GetShape()
                w, h = mil(size.x / 1e6), mil(size.y / 1e6)
                if shape == pcbnew.PAD_SHAPE_CIRCLE:
                    default_pad = {"padType": "ELLIPSE", "width": w, "height": h}
                elif shape == pcbnew.PAD_SHAPE_OVAL:
                    default_pad = {"padType": "OVAL", "width": w, "height": h}
                elif shape in (pcbnew.PAD_SHAPE_ROUNDRECT,
                               pcbnew.PAD_SHAPE_CHAMFERED_RECT):
                    default_pad = {"padType": "ROUND_RECT", "width": w, "height": h,
                                   "radius": mil(pad.GetRoundRectCornerRadius() / 1e6)}
                else:
                    default_pad = {"padType": "RECT", "width": w, "height": h}
                hole = None
                if drill.x > 0 and drill.y > 0:
                    round_hole = abs(drill.x - drill.y) <= 1000      # 1 µm 容差
                    hole = {"holeType": "ROUND" if round_hole else "SLOT",
                            "width": mil(drill.x / 1e6),
                            "height": mil(drill.y / 1e6)}
                px, py = abs_pt(pad.GetPosition())
                plated = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                board_pad += 1
                log.rec("PAD", uuid.uuid4().hex[:16], {
                    "partitionId": "", "groupId": 0,
                    "netName": pad.GetNetname(), "layerId":
                        (12 if plated else (1 if pad.IsOnLayer(pcbnew.F_Cu) else 2)),
                    "num": pad.GetNumber(), "centerX": px, "centerY": py,
                    "padAngle": round(pad.GetOrientationDegrees(), 3), "hole": hole,
                    "defaultPad": default_pad, "specialPad": [], "padOffsetX": 0,
                    "padOffsetY": 0, "relativeAngle": 0, "plated": plated,
                    "padType": ("TEST" if pad_type == "test" else "NORMAL"),
                    "topSolderExpansion": None,
                    "bottomSolderExpansion": None, "topPasteExpansion": None,
                    "bottomPasteExpansion": None, "locked": False, "zIndex": 7,
                    "connectMode": None, "spokeSpace": None, "spokeWidth": None,
                    "spokeAngle": None, "padLen": None})

            for item in (fp.GraphicalItems() if only in (None, "silk") else []):
                try:
                    layer = item.GetLayer()
                except Exception:
                    continue
                if layer not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                    continue
                if not hasattr(item, "GetStart"):
                    continue
                pts = [abs_pt(item.GetStart()), abs_pt(item.GetEnd())]
                board_silk += 1
                log.rec("POLY", uuid.uuid4().hex[:16], {
                    "partitionId": "", "groupId": 0, "netName": "",
                    "layerId": 3 if layer == pcbnew.F_SilkS else 4,
                    "width": (mil(item.GetWidth() / 1e6)
                              if hasattr(item, "GetWidth") else 10),
                    "path": _poly_path(pts), "locked": False, "zIndex": 6,
                    "polyType": "NORMAL"})

            if only not in (None, "text"):
                continue
            ref = fp.GetReference()
            rp = fp.GetPosition()
            board_text += 1
            log.rec("STRING", uuid.uuid4().hex[:16], {
                "partitionId": "", "groupId": 0, "layerId": 3,
                "x": mil(rp.x / 1e6), "y": -mil(rp.y / 1e6), "text": ref,
                "fontFamily": "default", "fontSize": mil(0.8), "strokeWidth": 8,
                "bold": 0, "italic": 0, "origin": "CENTER_MIDDLE", "angle": 0,
                "reverse": False, "expansion": 0, "mirror": False, "locked": False,
                "zIndex": -1, "specialColor": None})

    if placeholders:
        emit_placeholders(log, {"LINE": n_line, "VIA": n_via,
                                "POLY": n_poly + n_silk + board_silk,
                                "FILL": n_fill, "COMPONENT": 0,
                                "PAD": board_pad, "ATTR": 0,
                                "PAD_NET": 0, "NET": len(nets),
                                "STRING": board_text, "POUR": 0})

    # ============ FOOTPRINT + COMPONENT ============
    fp_count = pad_count = 0
    if with_components:
        fp_uuid_by_ref = {}
        for fp in board.GetFootprints():
            fp_uuid = uuid.uuid4().hex[:16]
            fp_uuid_by_ref[fp.GetReference()] = fp_uuid
            log.dochead("FOOTPRINT", fp_uuid)
            emit_layers(log, {1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 48, 49})
            emit_placeholders(log, {"PAD": 400, "POLY": 800, "FILL": 200, "ATTR": 50})
            log.rec("NET", ["NET", ""],
                    {"netType": None, "specialColor": None, "retLine": True,
                     "differentialName": None, "isPositiveNet": False,
                     "equalLengthGroupName": None})
            log.rec("META", "META", {"title": fp.GetFPIDAsString(), "zIndex": 3})
            log.rec("ATTR", uuid.uuid4().hex[:16], {
                "partitionId": "", "groupID": 0, "parentId": "", "layerId": 3,
                "x": None, "y": None, "key": "Footprint",
                "value": fp.GetFPIDAsString(), "keyVisible": False,
                "valueVisible": False, "fontFamily": "default", "fontSize": 45,
                "strokeWidth": 6, "bold": 0, "italic": 0, "origin": "LEFT_BOTTOM",
                "angle": 0, "reverse": False, "expansion": 0, "mirror": False,
                "locked": False, "zIndex": 15})

            origin = fp.GetPosition()

            def rel(pt):
                return (mil((pt.x - origin.x) / 1e6), -mil((pt.y - origin.y) / 1e6))

            for pad in (fp.Pads() if only in (None, "pads") else []):
                size, drill = pad.GetSize(), pad.GetDrillSize()
                plated_attr = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                if pad_subset == "smd" and plated_attr:
                    continue
                if pad_subset == "tht" and not plated_attr:
                    continue
                shape = pad.GetShape()
                w, h = mil(size.x / 1e6), mil(size.y / 1e6)
                if shape == pcbnew.PAD_SHAPE_CIRCLE:
                    default_pad = {"padType": "ELLIPSE", "width": w, "height": h}
                elif shape == pcbnew.PAD_SHAPE_OVAL:
                    default_pad = {"padType": "OVAL", "width": w, "height": h}
                elif shape in (pcbnew.PAD_SHAPE_ROUNDRECT,
                               pcbnew.PAD_SHAPE_CHAMFERED_RECT):
                    default_pad = {"padType": "ROUND_RECT", "width": w, "height": h,
                                   "radius": mil(pad.GetRoundRectCornerRadius() / 1e6)}
                else:
                    default_pad = {"padType": "RECT", "width": w, "height": h}
                hole = None
                if drill.x > 0 and drill.y > 0:
                    round_hole = abs(drill.x - drill.y) <= 1000      # 1 µm 容差
                    hole = {"holeType": "ROUND" if round_hole else "SLOT",
                            "width": mil(drill.x / 1e6),
                            "height": mil(drill.y / 1e6)}
                px, py = rel(pad.GetPosition())
                plated = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                pad_count += 1
                log.rec("PAD", uuid.uuid4().hex[:16], {
                    "partitionId": "", "groupId": 0, "netName": "",
                    "layerId": 12 if plated else (1 if pad.IsOnLayer(pcbnew.F_Cu) else 2),
                    "num": pad.GetNumber(), "centerX": px, "centerY": py,
                    "padAngle": round(pad.GetOrientationDegrees(), 3), "hole": hole,
                    "defaultPad": default_pad, "specialPad": [], "padOffsetX": 0,
                    "padOffsetY": 0, "relativeAngle": 0, "plated": plated,
                    "padType": ("TEST" if pad_type == "test" else "NORMAL"),
                    "topSolderExpansion": None,
                    "bottomSolderExpansion": None, "topPasteExpansion": None,
                    "bottomPasteExpansion": None, "locked": False, "zIndex": 7,
                    "connectMode": None, "spokeSpace": None, "spokeWidth": None,
                    "spokeAngle": None, "padLen": None})

            for item in fp.GraphicalItems():
                try:
                    layer = item.GetLayer()
                except Exception:
                    continue
                if layer not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                    continue
                if not hasattr(item, "GetStart"):
                    continue
                pts = [rel(item.GetStart()), rel(item.GetEnd())]
                log.rec("POLY", uuid.uuid4().hex[:16], {
                    "partitionId": "", "groupId": 0, "netName": "",
                    "layerId": 3 if layer == pcbnew.F_SilkS else 4,
                    "width": (mil(item.GetWidth() / 1e6)
                              if hasattr(item, "GetWidth") else 10),
                    "path": _poly_path(pts), "locked": False, "zIndex": 6,
                    "polyType": "NORMAL"})
            fp_count += 1

        for fp in board.GetFootprints():
            pos = fp.GetPosition()
            cid = uuid.uuid4().hex[:16]
            log.rec("COMPONENT", cid, {
                "partitionId": "", "groupId": 0, "layerId": 1,
                "x": mil(pos.x / 1e6), "y": -mil(pos.y / 1e6),
                "angle": round(fp.GetOrientationDegrees(), 3),
                "attrs": {"Unique ID": cid[:8], "Reuse Block": "", "Group ID": "",
                          "Channel ID": ""},
                "zIndex": None})
            log.rec("ATTR", cid + "fp", {
                "partitionId": "", "groupID": 0, "parentId": cid, "layerId": 3,
                "x": None, "y": None, "key": "Footprint",
                "value": fp_uuid_by_ref[fp.GetReference()], "keyVisible": False,
                "valueVisible": False, "fontFamily": "default", "fontSize": 45,
                "strokeWidth": 6, "bold": 0, "italic": 0, "origin": "LEFT_BOTTOM",
                "angle": 0, "reverse": False, "expansion": 0, "mirror": False,
                "locked": False, "zIndex": 15})
            log.rec("ATTR", cid + "ds", {
                "partitionId": "", "groupID": 0, "parentId": cid, "layerId": 3,
                "x": None, "y": None, "key": "Designator",
                "value": fp.GetReference(), "keyVisible": False, "valueVisible": True,
                "fontFamily": "default", "fontSize": 45, "strokeWidth": 6,
                "bold": 0, "italic": 0, "origin": "LEFT_BOTTOM", "angle": 0,
                "reverse": False, "expansion": 0, "mirror": False, "locked": False,
                "zIndex": 16})
            for pad in fp.Pads():
                if pad.GetNetname():
                    log.rec("PAD_NET", ["PAD_NET", cid, pad.GetNumber(),
                                        uuid.uuid4().hex[:16]], {})

    # ============ 打包 ============
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{name}.epro2")
    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({
            "title": name, "cbb_project": False, "editorVersion": VERSION,
            "introduction": "", "description": "", "tags": "[]"},
            ensure_ascii=False, indent=1))
        z.writestr("IMAGE/", b"")
        z.writestr(f"{name}.epru", log.text())
    os.replace(tmp, out)
    print(f"生成：{out}")
    print(f"  记录 {len(log.lines)} 条：走线 {n_line}、过孔 {n_via}、板框 {n_poly}、"
          f"板级丝印 {n_silk}、铺铜 {n_fill}、封装文档 {fp_count}、焊盘 {pad_count}")
    print(f"  板上直放：焊盘 {board_pad}、封装丝印 {board_silk}、位号 {board_text}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="KiCad 板 → 嘉立创EDA 3.x 工程 .epro2")
    ap.add_argument("board")
    ap.add_argument("-o", "--out", default="out/epro2")
    ap.add_argument("--name", default=None)
    ap.add_argument("--with-components", action="store_true")
    ap.add_argument("--no-board-pads", dest="board_pads", action="store_false",
                    help="不在板上直接放焊盘/丝印（只走封装库路线）")
    ap.add_argument("--with-zones", action="store_true")
    ap.add_argument("--zone-tol", type=float, default=0.0,
                    help="铺铜多边形简化容差（mm，0=不简化）")
    ap.add_argument("--pad-subset", choices=["all", "smd", "tht"], default="all",
                    help="只放某一类板级焊盘（二分卡死用）")
    ap.add_argument("--pad-type", choices=["auto", "test"], default="auto",
                    help="板级焊盘的 padType（auto=NORMAL，test=TEST）")
    ap.add_argument("--only", choices=["pads", "silk", "text"], default=None,
                    help="只放某一类板级对象（二分卡死用）")
    ap.add_argument("--no-placeholders", dest="placeholders", action="store_false",
                    help="不输出 ELE_PLACEHOLDER（排查卡死用）")
    ap.add_argument("--zone-no-net", dest="zone_net", action="store_false",
                    help="铺铜不带网络（避免打开时做连通性/DRC 计算）")
    a = ap.parse_args(argv)
    name = a.name or os.path.splitext(os.path.basename(a.board))[0]
    build(a.board, a.out, name, with_components=a.with_components,
          with_zones=a.with_zones, board_pads=a.board_pads,
          zone_tol=a.zone_tol, zone_net=a.zone_net,
          placeholders=a.placeholders, only=a.only,
          pad_subset=a.pad_subset, pad_type=a.pad_type)
    return 0


if __name__ == "__main__":
    sys.exit(main())
