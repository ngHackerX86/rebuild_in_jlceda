"""生成嘉立创"飞针测试"数据文件 FlyingProbeTesting.json。

逆向自 EasyEDA Pro 的导出样例（本仓库 jlc_gerber/ 目录），字段含义说明：

  {
    "lengthUnit": "mil",
    "remark": [],
    "components": {fields:[COMPONENT_NO, COMPONENT_NAME, LAYER, X_COORDINATE,
                           Y_COORDINATE, ANGLE], rows:[...]},
    "pins":       {fields:[PIN_NO, PIN_NAME, PIN_X, PIN_Y, LAYER, PIN_TYPE,
                           NET_NAME, NET_TYPE, PAD_SHAPE, PAD_SIZEX, PAD_SIZEY,
                           HOLE_SIZE, HOLE_LEN, PAD_ANGLE], rows:[...]}
  }

观察结论：
  * 坐标单位 mil，原点与 Gerber 相同（Y 轴与 KiCad 相反，取负）；
  * LAYER 取 "T"/"B"；
  * PIN_TYPE 取 "SMD"/"DIP"/"TP"（测试点单独标 TP）；
  * NET_TYPE 只在电源/地网络上有值（样例中为 "GND"），其余为空串；
  * PAD_SHAPE 取 "R"/"O"/"C"，尺寸为 mil。

⚠ 本文件不是制板必需项（嘉立创解析 Gerber 出板不依赖它），只在用其
  飞针测试/电气测试服务时需要。字段语义为逆向推断，正式发布前应与
  嘉立创确认；生成失败不应阻断流程。
"""
from __future__ import annotations

import json
import os

MIL = 39.37007874015748
GND_NAMES = {"GND", "AGND", "DGND", "GNDD", "VSS", "0", "EARTH"}


def _shape_code(pad) -> str:
    import pcbnew
    shapes = {}
    for name in ("PAD_SHAPE_CIRCLE", "PAD_SHAPE_RECT", "PAD_SHAPE_OVAL",
                 "PAD_SHAPE_ROUNDRECT", "PAD_SHAPE_CHAMFERED_RECT"):
        if hasattr(pcbnew, name):
            shapes[getattr(pcbnew, name)] = name
    s = shapes.get(pad.GetShape(), "PAD_SHAPE_OTHER")
    return {
        "PAD_SHAPE_CIRCLE": "C",
        "PAD_SHAPE_OVAL": "O",
        "PAD_SHAPE_RECT": "R",
        "PAD_SHAPE_ROUNDRECT": "R",
        "PAD_SHAPE_CHAMFERED_RECT": "R",
    }.get(s, "R")


def build(board) -> dict:
    import pcbnew

    comp_rows = []
    pin_rows = []
    comp_no = 0
    pin_no = 0
    for fp in board.GetFootprints():
        comp_no += 1
        pos = fp.GetPosition()
        x = pos.x / 1_000_000.0 * MIL
        y = -pos.y / 1_000_000.0 * MIL
        on_top = fp.GetLayer() == pcbnew.F_Cu
        comp_rows.append([comp_no, fp.GetReference(), "T" if on_top else "B",
                          round(x, 4), round(y, 4),
                          round(fp.GetOrientationDegrees() % 360, 4)])
        for pad in fp.Pads():
            pin_no += 1
            p = pad.GetPosition()
            px = p.x / 1_000_000.0 * MIL
            py = -p.y / 1_000_000.0 * MIL
            ds = pad.GetDrillSize()
            has_hole = ds.x > 0 or ds.y > 0
            sz = pad.GetSize()
            netname = pad.GetNetname() or ""
            upper = netname.upper()
            if "TP" in fp.GetReference().upper() or fp.GetAttributes() & getattr(
                    pcbnew, "FP_EXCLUDE_FROM_POS_FILES", 0):
                ptype = "TP"
            elif has_hole:
                ptype = "DIP"
            else:
                ptype = "SMD"
            pin_rows.append([
                pin_no,
                f"{fp.GetReference()}_{pad.GetNumber()}",
                round(px, 4), round(py, 4),
                "T" if on_top else "B",
                ptype,
                netname,
                "GND" if upper in GND_NAMES else "",
                _shape_code(pad),
                round(sz.x / 1_000_000.0 * MIL, 4),
                round(sz.y / 1_000_000.0 * MIL, 4),
                round(ds.x / 1_000_000.0 * MIL, 4) if has_hole else 0,
                round(ds.y / 1_000_000.0 * MIL, 4) if has_hole else 0,
                round(pad.GetOrientationDegrees() % 360, 4),
            ])

    return {
        "lengthUnit": "mil",
        "remark": [],
        "components": {
            "fields": ["COMPONENT_NO", "COMPONENT_NAME", "LAYER", "X_COORDINATE",
                       "Y_COORDINATE", "ANGLE"],
            "rows": comp_rows,
        },
        "pins": {
            "fields": ["PIN_NO", "PIN_NAME", "PIN_X", "PIN_Y", "LAYER", "PIN_TYPE",
                       "NET_NAME", "NET_TYPE", "PAD_SHAPE", "PAD_SIZEX", "PAD_SIZEY",
                       "HOLE_SIZE", "HOLE_LEN", "PAD_ANGLE"],
            "rows": pin_rows,
        },
    }


def write(board, outdir: str) -> str | None:
    try:
        data = build(board)
    except Exception:
        return None
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "FlyingProbeTesting.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    return path
