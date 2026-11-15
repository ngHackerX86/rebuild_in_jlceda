# -*- coding: utf-8 -*-
"""KiCad 环境用例：用自造板验证 F02 / F03 / F04 / F08 的转换行为。

需要带 pcbnew 的解释器（KiCad 自带 python）：
    & "<KiCad 安装目录>\\bin\\python.exe" -m pytest tests/kicad -q
或直接：
    & "...\\python.exe" tests\\kicad\\test_conversion_fixtures.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fixtures"))

TEMPLATE = REPO / "rebuild_in_jlc" / "templates" / "PCB1_min.epcb2"


def _convert(board: Path, out: Path, name: str, extra=()) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(REPO / "tools" / "kicad2eprj3.py"), str(board),
           "-o", str(out), "--name", name, "--template", str(TEMPLATE), *extra]
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _records(out: Path, name: str, doc: str = "PCB"):
    """读回生成的记录（type, payload），doc 指定文档类型（PCB / FOOTPRINT）。"""
    f = next((out / f"{name}_eprj3" / "pcb").glob("*.epcb2"))
    cur = None
    for line in f.read_text(encoding="utf-8").splitlines():
        head, _, body = line.partition("}||")
        if not body.strip():
            continue
        try:
            h = json.loads(head + "}")
            d = json.loads(body.rstrip("|\n"))
        except Exception:
            continue
        if h.get("type") == "DOCHEAD":
            cur = d.get("docType")
            continue
        if cur == doc:
            yield h.get("type"), d


def _poly_centers(payload):
    flat = [v for v in payload["path"][0] if isinstance(v, (int, float))]
    pts = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
    if not pts:
        return None
    # 闭合多边形首点重复，用去重后的点算中心
    uniq = pts[:-1] if pts[0] == pts[-1] else pts
    return (sum(p[0] for p in uniq) / len(uniq), sum(p[1] for p in uniq) / len(uniq))


def main() -> int:
    import make_fixtures as mf

    failed = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fx = tmp / "fixtures"
        fx.mkdir()
        for name, fn in mf.BUILDERS.items():
            fn(fx / name)

        # ---- F02：无铺铜板 ----
        out = tmp / "nozone"
        r = _convert(fx / "no_zone.kicad_pcb", out, "NoZone")
        ok = r.returncode == 0 and "POUR=0" in (r.stdout + r.stderr)
        print(f"  [{'PASS' if ok else 'FAIL'}] F02 无铺铜板转换成功且提示 POUR=0"
              f"（exit={r.returncode}）")
        if not ok:
            print("        ", (r.stdout + r.stderr).strip().splitlines()[-3:])
        failed += 0 if ok else 1

        # ---- F03：同封装两次 → 两个钢网 FILL ----
        out = tmp / "paste"
        r = _convert(fx / "repeated_paste.kicad_pcb", out, "Paste")
        fills = [p for t, p in _records(out, "Paste") if t == "FILL"]
        centers = sorted(round(_poly_centers(p)[0], 1) for p in fills if _poly_centers(p))
        ok = len(fills) == 2 and len(set(centers)) == 2
        print(f"  [{'PASS' if ok else 'FAIL'}] F03 同封装两次 → {len(fills)} 个钢网 FILL"
              f"，中心 X={centers}")
        failed += 0 if ok else 1

        # ---- F04：印膏扩展写成具体值，且两实例不合并 ----
        out = tmp / "exp"
        r = _convert(fx / "paste_expansion.kicad_pcb", out, "Exp")
        pads = [p for t, p in _records(out, "Exp", "FOOTPRINT") if t == "PAD"]
        exps = sorted({round(p.get("topPasteExpansion") or 0, 3) for p in pads})
        text = next((out / "Exp_eprj3" / "pcb").glob("*.epcb2")).read_text(encoding="utf-8")
        n_fp = text.count('"docType":"FOOTPRINT"')
        ok = exps == [3.937, 11.811] and n_fp == 2
        print(f"  [{'PASS' if ok else 'FAIL'}] F04 扩展={exps}（期望 [3.937, 11.811]），"
              f"封装变体 {n_fp} 个（期望 2）")
        failed += 0 if ok else 1

        # ---- 圆丝印：封装内整圆必须落到板级 POLY（测试点外圈丢失的回归） ----
        out = tmp / "circle"
        r = _convert(fx / "silk_circle.kicad_pcb", out, "Circle")
        circles = []
        for _t2, p in _records(out, "Circle", "FOOTPRINT"):
            if _t2 != "POLY" or p.get("layerId") != 3:
                continue
            flat = [v for v in (p.get("path") or []) if isinstance(v, (int, float))]
            n = len(flat) // 2
            if 16 <= n <= 200:          # 实测 >180 会被 EDA 丢弃，这里同时卡上限
                circles.append(n)
        ok = bool(circles)
        print(f"  [{'PASS' if ok else 'FAIL'}] 封装内圆丝印 POLY（{len(circles)} 个，"
              f"点数 {circles[:3]}，须 ≤200）")
        failed += 0 if ok else 1


        # ---- N03：圆形仅钢网焊盘的窗口必须按形状生成（面积 ≈ 0.7854 mm²） ----
        out = tmp / "pc"
        r = _convert(fx / "paste_circle.kicad_pcb", out, "PasteCircle")
        import math as _m
        areas = []
        for _t3, p in _records(out, "PasteCircle", "PCB"):
            if _t3 != "FILL" or p.get("layerId") != 7:
                continue
            flat = [v for v in p["path"][0] if isinstance(v, (int, float))]
            pts = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
            s2 = sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
                     for i in range(len(pts) - 1))
            areas.append(abs(s2) / 2 * (0.0254 ** 2))
        ok = bool(areas) and abs(areas[0] - _m.pi * 0.25) / (_m.pi * 0.25) < 0.02
        print(f"  [{'PASS' if ok else 'FAIL'}] N03 圆形钢网窗口面积={areas[:1]}"
              f"（期望 ≈0.7854 mm²）")
        failed += 0 if ok else 1

        # ---- N01：同号两枚焊盘，PAD_NET 必须带 padId ----
        out = tmp / "mp"
        r = _convert(fx / "mixed_padnet.kicad_pcb", out, "Mixed")
        ids = []
        pad_ids = []
        for _t4, p in _records(out, "Mixed", "PCB"):
            if _t4 == "PAD_NET":
                import json as _json
                key = _json.loads(p if isinstance(p, str) else "[]")
        # 直接从文件里读 PAD_NET 的 id
        import json as _json
        f2 = next((out / "Mixed_eprj3" / "pcb").glob("*.epcb2"))
        for line in f2.read_text(encoding="utf-8").splitlines():
            head, _, body = line.partition("}||")
            if '"type":"PAD_NET"' in head:
                ids.append(_json.loads(head + "}")["id"])
        for line in f2.read_text(encoding="utf-8").splitlines():
            head, _, body = line.partition("}||")
            if '"type":"PAD"' in head:
                pad_ids.append(_json.loads(head + "}").get("id"))
        ok = bool(ids) and all(_json.loads(i)[3] for i in ids) and len(pad_ids) == 2
        print(f"  [{'PASS' if ok else 'FAIL'}] N01 PAD_NET 带 padId：{ids[:1]}，封装焊盘 {len(pad_ids)} 枚")
        failed += 0 if ok else 1

        # ---- F08：改名内层仍映射到 15 ----
        out = tmp / "inner"
        r = _convert(fx / "renamed_inner.kicad_pcb", out, "Inner")
        layers = [p.get("layerId") for t, p in _records(out, "Inner") if t == "LINE"]
        ok = 15 in layers and 13 not in layers
        print(f"  [{'PASS' if ok else 'FAIL'}] F08 改名内层 → layerId={sorted(set(layers))}"
              f"（期望含 15、不含 13）")
        failed += 0 if ok else 1

    print("全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


def test_conversion_fixtures():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
