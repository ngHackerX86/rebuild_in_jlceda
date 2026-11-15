# -*- coding: utf-8 -*-
"""F10：对照工具的口径（正常 / 缺内铜参考 / 缺钻孔 / 整体偏移）。

用**人造 Gerber 文本**造场景，不依赖 KiCad 与 EDA。
运行：python tests/offline/test_compare_states.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import compare_gerber_layers as cmp  # noqa: E402

HEADER = "%FSLAX46Y46*%\n%MOMM*%\nG04 test*\n"
PTS = [(10, 10), (20, 10), (20, 20), (10, 20)]


def gerber(points, shift=0.0):
    out = [HEADER, "G01*"]
    for x, y in points:
        out.append(f"X{int((x + shift) * 1e6)}Y{int(y * 1e6)}D03*\n")
    out.append("M02*\n")
    return "".join(out)


def drill(holes):
    out = ["M48\n"]
    tools = sorted({d for _x, _y, d in holes})
    for i, d in enumerate(tools, start=1):
        out.append(f"T{i}C{d:.3f}\n")
    out.append("%\n")
    for i, d in enumerate(tools, start=1):
        out.append(f"T{i}\n")
        for x, y, dd in [h for h in holes if h[2] == d]:
            out.append(f"X{x:.3f}Y{y:.3f}\n")
    out.append("M30\n")
    return "".join(out)


BASE_MEMBERS = {
    "Gerber_TopLayer.GTL": ("F_Cu.gtl", True),
    "Gerber_BottomLayer.GBL": ("B_Cu.gbl", True),
    "Gerber_BoardOutlineLayer.GKO": ("Edge_Cuts.gm1", True),
    "Gerber_TopSilkscreenLayer.GTO": ("F_Silkscreen.gto", True),
    "Gerber_BottomSilkscreenLayer.GBO": ("B_Silkscreen.gbo", True),
    "Gerber_TopSolderMaskLayer.GTS": ("F_Mask.gts", True),
    "Gerber_BottomSolderMaskLayer.GBS": ("B_Mask.gbs", True),
    "Gerber_TopPasteMaskLayer.GTP": ("F_Paste.gtp", True),
}


def build_case(tmp: Path, *, eda_inner=False, ref_inner=False,
               eda_npth=False, ref_npth=True, shift=0.0):
    eda = tmp / "eda.zip"
    ref = tmp / "ref"
    ref.mkdir(parents=True, exist_ok=True)

    members = {}
    for member, (suffix, _) in BASE_MEMBERS.items():
        s = shift if member == "Gerber_TopLayer.GTL" else 0.0
        members[member] = gerber(PTS, s)
    members["Drill_PTH_Through.DRL"] = drill([(10, 10, 0.3), (20, 20, 0.6)])
    if eda_inner:
        members["Gerber_InnerLayer1.G1"] = gerber(PTS)
    if eda_npth:
        members["Drill_NPTH_Through.DRL"] = drill([(15, 15, 3.2)])
    with zipfile.ZipFile(eda, "w") as zf:
        for k, v in members.items():
            zf.writestr(k, v)

    for member, (suffix, _) in BASE_MEMBERS.items():
        (ref / suffix).write_text(gerber(PTS), encoding="utf-8")
    (ref / "PTH.drl").write_text(drill([(10, 10, 0.3), (20, 20, 0.6)]), encoding="utf-8")
    if ref_inner:
        (ref / "In1_Cu.g1").write_text(gerber(PTS), encoding="utf-8")
    if ref_npth:
        (ref / "NPTH.drl").write_text(drill([(15, 15, 3.2)]), encoding="utf-8")
    return eda, ref


def run_case(name, expect_code, expect_states, **kw) -> bool:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        eda, ref = build_case(tmp, **kw)
        js = tmp / "out.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cmp.main([str(eda), str(ref), "--json", str(js)])
        data = json.loads(js.read_text(encoding="utf-8"))
        got = {k: v["status"] for k, v in data["layers"].items()}
        bad = [f"{k}={got.get(k)}（期望 {v}）" for k, v in expect_states.items()
               if got.get(k) != v]
        ok = code == expect_code and not bad
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: exit={code}（期望 {expect_code}）")
        for b in bad:
            print("         " + b)
        if not ok and code != expect_code:
            print("         摘要:", data["summary"])
        return ok


CASES = [
    ("两层板齐全（两侧都有 PTH/NPTH）",
     0, {"F_Cu": "compared", "PTH": "compared", "NPTH": "compared",
         "In1_Cu": "not_applicable"},
     dict(eda_npth=True, ref_npth=True)),
    ("四层板：EDA 有内层、参考缺内层",
     3, {"In1_Cu": "missing_evidence"},
     dict(eda_inner=True, ref_inner=False, eda_npth=True, ref_npth=True)),
    ("EDA 缺 NPTH 钻孔文件",
     3, {"NPTH": "missing_evidence"},
     dict(eda_npth=False, ref_npth=True)),
    ("整体平移 2mm",
     4, {"F_Cu": "differs", "B_Cu": "compared"},
     dict(eda_npth=True, ref_npth=True, shift=2.0)),
]


def main() -> int:
    ok = sum(run_case(*c[:3], **c[3]) for c in CASES)
    print(f"{ok}/{len(CASES)} 通过")
    return 0 if ok == len(CASES) else 1


def test_compare_states():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
