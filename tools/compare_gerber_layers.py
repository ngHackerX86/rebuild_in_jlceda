# -*- coding: utf-8 -*-
"""逐层对照：嘉立创 EDA 导出包 vs KiCad 自己导出的 Gerber/钻孔。

用法：
    python tools/compare_gerber_layers.py <eda_gerber.zip> <kicad_ref_dir> \
        [--source-copper N] [--json report.json]

判定口径（每层一个状态）：

| 状态 | 含义 |
|---|---|
| `compared`      | 两边都有，统计项在容差内 |
| `differs`       | 两边都有，但包围盒整体偏移 > 阈值（疑似整体平移/缩放） |
| `missing_evidence` | **应比而对不上**：源板有这层却缺参考，或 EDA 包里缺该层 |
| `not_applicable`   | 源板确实没有这层（需要 `--source-copper` 才能确定） |
| `parse_error`   | 文件读到了但解析不出坐标（不是"零差异"） |

退出码：0 全部 `compared`（允许统计差异，会在表里标 ⚠）；
        3 存在 `missing_evidence` / `parse_error`；4 存在 `differs`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile

# EDA 条目 → KiCad 文件名后缀
MAP = {
    "Gerber_TopLayer.GTL": "F_Cu.gtl",
    "Gerber_BottomLayer.GBL": "B_Cu.gbl",
    "Gerber_InnerLayer1.G1": "In1_Cu.g1",
    "Gerber_InnerLayer2.G2": "In2_Cu.g2",
    "Gerber_TopSilkscreenLayer.GTO": "F_Silkscreen.gto",
    "Gerber_BottomSilkscreenLayer.GBO": "B_Silkscreen.gbo",
    "Gerber_TopSolderMaskLayer.GTS": "F_Mask.gts",
    "Gerber_BottomSolderMaskLayer.GBS": "B_Mask.gbs",
    "Gerber_TopPasteMaskLayer.GTP": "F_Paste.gtp",
    "Gerber_BottomPasteMaskLayer.GBP": "B_Paste.gbp",
    "Gerber_BoardOutlineLayer.GKO": "Edge_Cuts.gm1",
}

INNER_MEMBERS = ("Gerber_InnerLayer1.G1", "Gerber_InnerLayer2.G2")
SHIFT_MM = 0.5          # 包围盒整体偏移超过它，判为 differs


def read_gerber_text(path, is_zip=False, member=None):
    if is_zip:
        return zipfile.ZipFile(path).read(member).decode("utf-8", "replace")
    return open(path, encoding="utf-8", errors="replace").read()


def div_of(txt: str) -> float:
    m = re.search(r"%FSLAX(\d)(\d)Y\d\d\*%", txt)
    return 10.0 ** int(m.group(2)) if m else 1e5


def _body_lines(txt: str) -> str:
    """去掉 %...% 扩展指令行（%FSLAX46Y46*% 里的 "X46" 会被当成坐标，导致"空层"误判）。"""
    return "\n".join(ln for ln in txt.splitlines() if not ln.startswith("%"))


def stats(txt: str):
    div = div_of(txt)                      # FS 头要在剥离之前读
    has_fs = bool(re.search(r"%FSLAX", txt))
    body = _body_lines(txt)
    xs = [int(v) / div for v in re.findall(r"X(-?\d+)", body)]
    ys = [int(v) / div for v in re.findall(r"Y(-?\d+)", body)]
    bbox = (round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)) if xs else None
    return {
        "flash": txt.count("D03*"),
        "region": txt.count("G36*"),
        "line": txt.count("D01*"),
        "size": len(txt),
        "bbox": bbox,
        "has_fs": has_fs,
        "points": min(len(xs), len(ys)),
    }


def drill_stats(txt: str):
    tools = re.findall(r"^T\d+C([\d.]+)", txt, re.M)
    holes = len(re.findall(r"^X-?[\d.]+Y-?[\d.]+$", txt, re.M))
    slots = txt.count("G85")
    return {"tools": sorted(set(tools), key=float), "holes": holes, "slots": slots,
            "parse_ok": bool(tools) or holes > 0}


def _find(files, suffix):
    """按后缀找参考层；PTH 不能被 NPTH 命中（'NPTH.drl' 也以 'PTH.drl' 结尾）。"""
    for f in files:
        if suffix.endswith("PTH.drl") and not suffix.startswith("N"):
            if f.endswith("PTH.drl") and not f.endswith("NPTH.drl"):
                return f
            continue
        if f.endswith(suffix):
            return f
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="逐层对照 EDA 与 KiCad 的 Gerber")
    ap.add_argument("eda_zip")
    ap.add_argument("kicad_ref_dir")
    ap.add_argument("--source-copper", type=int, default=None,
                    help="源板铜层数（2/4/6…）。给了才能确定内层是“不适用”还是“缺证据”")
    ap.add_argument("--json", default=None, help="把逐层状态写成 JSON")
    a = ap.parse_args(argv)

    if not os.path.exists(a.eda_zip):
        print(f"EDA 导出包不存在：{a.eda_zip}")
        return 2
    if not os.path.isdir(a.kicad_ref_dir):
        print(f"KiCad 参考目录不存在：{a.kicad_ref_dir}")
        return 2

    files = os.listdir(a.kicad_ref_dir)
    zf = zipfile.ZipFile(a.eda_zip)
    members = set(zf.namelist())
    required_inner = max(0, (a.source_copper or 2) - 2)
    if a.source_copper is None:
        # 没声明源板层数：只要 EDA 包里有内层文件，就说明源板有内层 → 必须比
        present_inner = [m for m in INNER_MEMBERS if m in members]
        required_inner = len(present_inner)

    print("# 图元统计摘要（非几何等价证明）")
    print()
    if a.source_copper is None:
        print("> 未声明源板铜层数（`--source-copper`）：内层是否“不适用”无法确定，"
              "本次按 EDA 包实际存在的内层文件推断。")
        print()
    print("| 层 | 项目 | EDA | KiCad | 差异 |")
    print("|---|---|---|---|---|")

    rows = []
    results = {}
    for idx, (member, suffix) in enumerate(MAP.items()):
        name = suffix.split(".")[0]
        is_inner = member in INNER_MEMBERS
        inner_index = INNER_MEMBERS.index(member) + 1 if is_inner else 0
        if is_inner:
            # 源板可能把内层改名为 GND1_Cu/PWR 等：按 .g<序号> 扩展名找参考，而不是按名字
            suffix = f".g{inner_index}"


        # 1) 源板有没有这层
        if is_inner and inner_index > required_inner:
            results[name] = {"status": "not_applicable",
                             "note": f"源板只有 {a.source_copper or required_inner + 2} 层"}
            continue

        eda_has = member in members
        kname = _find(files, suffix)
        kpath = os.path.join(a.kicad_ref_dir, kname) if kname else None

        # 参考侧存在但**没有任何坐标**（空层，例如单面贴装板的 B.Paste）→ 不适用
        k_empty = False
        if kpath:
            _k = stats(read_gerber_text(kpath))
            k_empty = _k["points"] in (0, False) or _k["points"] == 0
        if k_empty and not eda_has:
            results[name] = {"status": "not_applicable", "note": "参考层为空且 EDA 无该层"}
            print(f"| {name} | - | 无 | 空 | 该层无对象，不适用 |")
            continue
        if not eda_has and not kpath:
            # 两侧都没有：内层由 --source-copper 决定是否"应有"，其余层视为本板不适用
            status = "missing_evidence" if is_inner else "not_applicable"
            results[name] = {"status": status, "note": "两侧都没有该层"}
            print(f"| {name} | - | 缺 | 缺 | 两侧都缺（{status}）|")
            continue
        if not eda_has:
            results[name] = {"status": "missing_evidence", "note": "EDA 包缺该层"}
            print(f"| {name} | - | 缺 | 有 | EDA 包缺该层 |")
            continue
        if not kpath:
            results[name] = {"status": "missing_evidence", "note": "参考目录缺该层"}
            print(f"| {name} | - | 有 | 缺 | 参考缺该层（未取得比对证据）|")
            continue

        e = stats(read_gerber_text(a.eda_zip, True, member))
        k = stats(read_gerber_text(kpath))
        if not e["has_fs"] or not k["has_fs"]:
            results[name] = {"status": "parse_error", "note": "缺少 %FSLAX 头，无法解析"}
            print(f"| {name} | - | {e['size']}B | {k['size']}B | 解析失败 |")
            continue

        shift = None
        if e["bbox"] and k["bbox"]:
            shift = (round(e["bbox"][0] - k["bbox"][0], 2),
                     round(e["bbox"][1] - k["bbox"][1], 2),
                     round(e["bbox"][2] - k["bbox"][2], 2),
                     round(e["bbox"][3] - k["bbox"][3], 2))
        status = "compared"
        if shift and max(abs(v) for v in shift) > SHIFT_MM:
            status = "differs"

        for key, label in (("flash", "闪绘 D03"), ("region", "区域 G36"),
                           ("line", "线段 D01"), ("size", "字节")):
            ev, kv = e[key], k[key]
            diff = ev - kv
            flag = "" if abs(diff) <= max(5, 0.05 * kv) else "⚠"
            print(f"| {name} | {label} | {ev} | {kv} | {diff:+d} {flag} |")
        results[name] = {"status": status, "shift_mm": shift,
                         "eda": {k2: e[k2] for k2 in ("flash", "region", "line", "size")},
                         "kicad": {k2: k[k2] for k2 in ("flash", "region", "line", "size")}}
        rows.append((name, e, k, status))

    print()
    print("| 层 | EDA 包围盒(mm) | KiCad 包围盒(mm) | 状态 |")
    print("|---|---|---|---|")
    for name, e, k, status in rows:
        print(f"| {name} | {e['bbox']} | {k['bbox']} | {status} |")

    # ---- 钻孔 ----
    for member, suffix in (("Drill_PTH_Through.DRL", "PTH.drl"),
                           ("Drill_NPTH_Through.DRL", "NPTH.drl")):
        name = suffix.split(".")[0]
        if member not in members:
            results[name] = {"status": "missing_evidence", "note": "EDA 包缺钻孔文件"}
            continue
        kname = _find(files, suffix)
        e = drill_stats(zf.read(member).decode("utf-8", "replace"))
        print()
        print(f"**{member}**")
        print(f"- EDA  ：刀具 {e['tools']} 孔数 {e['holes']} 槽 {e['slots']}")
        if not kname:
            results[name] = {"status": "missing_evidence", "note": "参考目录缺钻孔文件"}
            print("- KiCad：缺参考文件（未取得比对证据）")
            continue
        k = drill_stats(open(os.path.join(a.kicad_ref_dir, kname),
                             encoding="utf-8", errors="replace").read())
        print(f"- KiCad：刀具 {k['tools']} 孔数 {k['holes']} 槽 {k['slots']}")
        try:
            et = sorted(float(v) for v in e["tools"])
            kt = sorted(float(v) for v in k["tools"])
        except ValueError:
            et, kt = e["tools"], k["tools"]
        same = (et == kt and e["holes"] == k["holes"] and e["slots"] == k["slots"])
        results[name] = {"status": "compared" if same else "differs",
                         "eda": {k2: e[k2] for k2 in ("tools", "holes", "slots")},
                         "kicad": {k2: k[k2] for k2 in ("tools", "holes", "slots")}}

    # ---- 汇总 ----
    na = [n for n, r in results.items() if r["status"] == "not_applicable"]
    miss = [n for n, r in results.items() if r["status"] == "missing_evidence"]
    bad = [n for n, r in results.items() if r["status"] == "parse_error"]
    diff = [n for n, r in results.items() if r["status"] == "differs"]

    print()
    print("## 结论")
    print()
    print(f"- 已比较：{len([n for n, r in results.items() if r['status'] == 'compared'])} 层/项")
    if na:
        print(f"- 源板没有该层（不适用）：{'、'.join(na)}")
    if miss:
        print(f"- **缺证据**：{'、'.join(miss)} —— 这些层没有比对证据，结果不能视为等价")
    if bad:
        print(f"- **解析失败**：{'、'.join(bad)} —— 文件存在但读不出坐标")
    if diff:
        print(f"- **存在差异**：{'、'.join(diff)} —— 包围盒偏移超过 {SHIFT_MM} mm")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"source_copper": a.source_copper,
                       "shift_threshold_mm": SHIFT_MM,
                       "layers": results,
                       "summary": {"not_applicable": na, "missing_evidence": miss,
                                   "parse_error": bad, "differs": diff}}, fh,
                      ensure_ascii=False, indent=1)
        print(f"\nJSON 报告：{a.json}")

    if miss or bad:
        return 3
    if diff:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
