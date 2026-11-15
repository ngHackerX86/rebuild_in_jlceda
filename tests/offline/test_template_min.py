# -*- coding: utf-8 -*-
"""模板最小化：模板里不得残留设计记录，且最小化结果可复现。

运行：python tests/offline/test_template_min.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import minimize_template as mt  # noqa: E402

TEMPLATE = REPO / "rebuild_in_jlc" / "templates" / "PCB1_min.epcb2"


def main() -> int:
    failed = 0

    ok = TEMPLATE.exists()
    print(f"  [{'PASS' if ok else 'FAIL'}] 默认模板存在：{TEMPLATE.name}")
    if not ok:
        return 1

    problems = mt.check(TEMPLATE)
    ok = not problems
    print(f"  [{'PASS' if ok else 'FAIL'}] 模板不含设计记录（{len(problems)} 处残留）")
    if not ok:
        failed += 1
        for p in sorted(set(problems))[:5]:
            print("         " + p)

    size_kb = TEMPLATE.stat().st_size / 1024
    ok = size_kb < 100
    print(f"  [{'PASS' if ok else 'FAIL'}] 模板体积 {size_kb:.0f} KB < 100 KB（原模板 530 KB）")
    failed += 0 if ok else 1

    # 只保留 boilerplate：至少要有 LAYER / LAYER_PHYS / RULE
    types = [h.get("type") for h, _b, _r in mt.split_records(TEMPLATE.read_text(encoding="utf-8"))]
    for need in ("LAYER", "LAYER_PHYS", "RULE", "PRIMITIVE"):
        ok = need in types
        print(f"  [{'PASS' if ok else 'FAIL'}] 保留必需记录：{need}")
        failed += 0 if ok else 1

    # 最小化是幂等的：对最小模板再跑一次，记录数不变
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "again.epcb2"
        info = mt.minimize(TEMPLATE, out)
        ok = info["kept"] == len(types) and not info["dropped"]
        print(f"  [{'PASS' if ok else 'FAIL'}] 幂等：再最小化后仍是 {info['kept']} 条"
              f"（丢弃 {sum(info['dropped'].values())} 条）")
        failed += 0 if ok else 1

    print(f"{'全部通过' if failed == 0 else f'{failed} 项失败'}")
    return 0 if failed == 0 else 1


def test_template_min():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
