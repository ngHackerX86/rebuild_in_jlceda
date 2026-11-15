# -*- coding: utf-8 -*-
"""生成回归用的自造小板（不需要账号，纯 pcbnew）。

运行（必须用带 pcbnew 的 KiCad python）：
    python tests/fixtures/make_fixtures.py [--out out/fixtures]

产出：
    no_zone.kicad_pcb          只有板框 + 1 个 SMD 焊盘，**没有任何 zone**（F02）
    repeated_paste.kicad_pcb   同一封装放两次，各自带一个 F.Paste-only 焊盘（F03）
    paste_expansion.kicad_pcb  焊盘带 0.1/0.3 mm 印膏扩展（F04）
    renamed_inner.kicad_pcb    四层板，In1 改名 PWR，上面有一条走线（F08）
    unconnected.kicad_pcb      两个不同网络的焊盘且不连通 → DRC 有 unconnected_items（F01）
    silk_circle.kicad_pcb      封装里带一个圆丝印环（测试点外圈），验证圆不被丢
    paste_circle.kicad_pcb     仅钢网的**圆形**焊盘（直径 1mm），验证钢网窗口按形状生成
    mixed_padnet.kicad_pcb     同号两枚焊盘：一枚 SIGNAL、一枚无网络（验证 PAD_NET 的 padId）
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pcbnew

MM = pcbnew.FromMM


def _frame(board, w_mm: float, h_mm: float):
    pts = [(0, 0, w_mm, 0), (w_mm, 0, w_mm, h_mm), (w_mm, h_mm, 0, h_mm), (0, h_mm, 0, 0)]
    for (x1, y1, x2, y2) in pts:
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pcbnew.VECTOR2I(MM(x1), MM(y1)))
        s.SetEnd(pcbnew.VECTOR2I(MM(x2), MM(y2)))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(MM(0.1))
        board.Add(s)


def _layerset(*layers):
    ls = pcbnew.LSET()
    for layer in layers:
        ls.AddLayer(layer)
    return ls


def _footprint(board, ref: str, x_mm: float, y_mm: float, paste_only: bool = False,
               paste_margin_mm: float = 0.0):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID("Test", "Pad1"))
    fp.SetReference(ref)
    fp.SetPosition(pcbnew.VECTOR2I(MM(x_mm), MM(y_mm)))
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
    pad.SetPosition(pcbnew.VECTOR2I(MM(x_mm), MM(y_mm)))
    if paste_only:
        # 只有钢网窗口，没有铜/阻焊
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(_layerset(pcbnew.F_Paste))
    else:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(_layerset(pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste))
    if paste_margin_mm:
        try:
            pad.SetLocalSolderPasteMargin(MM(paste_margin_mm))
        except Exception:
            pass
    fp.Add(pad)
    board.Add(fp)
    return fp


def make_no_zone(path: Path):
    b = pcbnew.NewBoard(str(path))
    _frame(b, 20, 15)
    _footprint(b, "R1", 5, 5)
    pcbnew.SaveBoard(str(path), b)


def make_repeated_paste(path: Path):
    b = pcbnew.NewBoard(str(path))
    _frame(b, 25, 15)
    _footprint(b, "MK1", 5, 7, paste_only=True)
    _footprint(b, "MK2", 15, 7, paste_only=True)
    pcbnew.SaveBoard(str(path), b)


def make_paste_expansion(path: Path):
    b = pcbnew.NewBoard(str(path))
    _frame(b, 20, 15)
    _footprint(b, "U1", 6, 6, paste_margin_mm=0.1)
    _footprint(b, "U2", 14, 6, paste_margin_mm=0.3)
    pcbnew.SaveBoard(str(path), b)


def make_renamed_inner(path: Path):
    b = pcbnew.NewBoard(str(path))
    b.SetCopperLayerCount(4)
    _frame(b, 20, 15)
    try:
        b.SetLayerName(pcbnew.In1_Cu, "PWR")
    except Exception:
        pass
    _footprint(b, "R1", 3, 7)
    _footprint(b, "R2", 17, 7)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(MM(3), MM(7)))
    t.SetEnd(pcbnew.VECTOR2I(MM(17), MM(7)))
    t.SetWidth(MM(0.25))
    t.SetLayer(pcbnew.In1_Cu)
    b.Add(t)
    pcbnew.SaveBoard(str(path), b)


def make_unconnected(path: Path):
    """两个焊盘同属 GND 但没有任何连接 → KiCad DRC 报 unconnected_items（≥2 pad 才算）。"""
    b = pcbnew.NewBoard(str(path))
    b.SetCopperLayerCount(2)
    _frame(b, 20, 15)
    for name in ("GND", "VCC"):
        ni = pcbnew.NETINFO_ITEM(b, name)
        b.Add(ni)
    for ref, x, net in (("R1", 4, "GND"), ("R2", 16, "GND")):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetFPID(pcbnew.LIB_ID("T", "P"))
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(MM(x), MM(7)))
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(MM(1.5), MM(1.5)))
        pad.SetDrillSize(pcbnew.VECTOR2I(MM(0.8), MM(0.8)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetPosition(pcbnew.VECTOR2I(MM(x), MM(7)))
        pad.SetNet(b.FindNet(net))
        fp.Add(pad)
        b.Add(fp)
    pcbnew.SaveBoard(str(path), b)


def make_mixed_padnet(path: Path):
    """同号两枚物理焊盘：一枚有网络、一枚空网络（N01 反例）。"""
    b = pcbnew.NewBoard(str(path))
    b.SetCopperLayerCount(2)
    _frame(b, 20, 15)
    ni = pcbnew.NETINFO_ITEM(b, "SIGNAL")
    b.Add(ni)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetFPID(pcbnew.LIB_ID("Test", "DualPad"))
    fp.SetReference("J1")
    fp.SetPosition(pcbnew.VECTOR2I(MM(10), MM(7)))
    for idx, (x, net) in enumerate(((8, "SIGNAL"), (12, None))):
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(_layerset(pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste))
        pad.SetPosition(pcbnew.VECTOR2I(MM(x), MM(7)))
        if net:
            pad.SetNet(b.FindNet(net))
        fp.Add(pad)
    b.Add(fp)
    pcbnew.SaveBoard(str(path), b)


def make_paste_circle(path: Path):
    """一个直径 1mm 的圆形 F.Paste-only 焊盘（无铜），检验钢网窗口形状。"""
    b = pcbnew.NewBoard(str(path))
    _frame(b, 20, 15)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetFPID(pcbnew.LIB_ID("Test", "PasteCircle"))
    fp.SetReference("MK1")
    fp.SetPosition(pcbnew.VECTOR2I(MM(10), MM(7)))
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(_layerset(pcbnew.F_Paste))
    pad.SetPosition(pcbnew.VECTOR2I(MM(10), MM(7)))
    fp.Add(pad)
    b.Add(fp)
    pcbnew.SaveBoard(str(path), b)


def make_silk_circle(path: Path):
    """封装内一个整圆丝印（测试点外圈）：半径 1mm、线宽 0.12mm。"""
    b = pcbnew.NewBoard(str(path))
    _frame(b, 20, 15)
    fp = pcbnew.FOOTPRINT(b)
    fp.SetFPID(pcbnew.LIB_ID("Test", "TP"))
    fp.SetReference("TP1")
    fp.SetPosition(pcbnew.VECTOR2I(MM(10), MM(7)))
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(1.5), MM(1.5)))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(_layerset(pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste))
    pad.SetPosition(pcbnew.VECTOR2I(MM(10), MM(7)))
    fp.Add(pad)
    ring = pcbnew.PCB_SHAPE(fp)
    ring.SetShape(pcbnew.SHAPE_T_CIRCLE)
    ring.SetCenter(pcbnew.VECTOR2I(MM(10), MM(7)))
    ring.SetEnd(pcbnew.VECTOR2I(MM(11), MM(7)))          # 半径 1mm
    ring.SetLayer(pcbnew.F_SilkS)
    ring.SetWidth(MM(0.12))
    fp.Add(ring)
    b.Add(fp)
    pcbnew.SaveBoard(str(path), b)


BUILDERS = {
    "mixed_padnet.kicad_pcb": make_mixed_padnet,
    "paste_circle.kicad_pcb": make_paste_circle,
    "silk_circle.kicad_pcb": make_silk_circle,
    "unconnected.kicad_pcb": make_unconnected,
    "no_zone.kicad_pcb": make_no_zone,
    "repeated_paste.kicad_pcb": make_repeated_paste,
    "paste_expansion.kicad_pcb": make_paste_expansion,
    "renamed_inner.kicad_pcb": make_renamed_inner,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成回归用自造板")
    ap.add_argument("--out", default="out/fixtures")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in BUILDERS.items():
        target = out / name
        if target.exists():
            target.unlink()
        fn(target)
        print(f"  {target}  ({target.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
