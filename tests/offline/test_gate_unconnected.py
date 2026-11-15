# -*- coding: utf-8 -*-
"""F01：Gerber 插件的放行判定必须把"未连接项"算进去。

不依赖 KiCad：直接构造 run_drc 的返回结构，喂给 Gate 的合并逻辑。
运行：python tests/offline/test_gate_unconnected.py   或 python -m pytest tests/offline -q
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from kicad_jlc_exporter.jlc.gate import GateDecision  # noqa: E402


def decide(drc: dict, waivers: dict | None = None) -> GateDecision:
    """复刻 pipeline 里 DRC 段对 Gate 的喂法（与 kicad_jlc_exporter/jlc/pipeline.py 一致）。"""
    g = GateDecision()
    if drc.get("drc_parse_ok") is False:
        g.add("DRC", "failed", "DRC 报告解析失败，结果不可用于放行")
    by_type = dict(drc.get("by_type") or {})
    remaining = sum(by_type.values())
    unconn = int(drc.get("unconnected") or 0)
    if remaining > 0:
        g.add("DRC", "failed", f"{remaining} 条 error 级违规：{sorted(by_type)}")
    if unconn > 0:
        ut = drc.get("unconnected_types") or {}
        detail = "、".join(f"{k}×{v}" for k, v in sorted(ut.items())) or f"{unconn} 项"
        g.add("DRC 连通性", "failed", f"仍有 {unconn} 个未连接项（{detail}）")
    if remaining == 0 and unconn == 0 and drc.get("drc_parse_ok") is not False:
        g.add("DRC", "passed", "0 违规、未连接 0")
    return g


CASES = [
    ("只有未连接项", {"ran": True, "violations": 0, "by_type": {},
                 "unconnected": 1, "unconnected_types": {"unconnected_items": 1},
                 "drc_parse_ok": True}, False),
    ("报告解析失败", {"ran": True, "violations": 0, "by_type": {},
                 "unconnected": 0, "drc_parse_ok": False}, False),
    ("干净板", {"ran": True, "violations": 0, "by_type": {},
              "unconnected": 0, "drc_parse_ok": True}, True),
    ("既有违规又有未连接", {"ran": True, "violations": 2, "by_type": {"Clearance": 2},
                   "unconnected": 3, "drc_parse_ok": True}, False),
]


def main() -> int:
    failed = 0
    for name, drc, expect in CASES:
        g = decide(drc)
        got = g.publishable
        ok = got == expect
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: publishable={got}（期望 {expect}）")
        if not ok:
            failed += 1
            print("         blockers:", g.blockers)
    print(f"{len(CASES) - failed}/{len(CASES)} 通过")
    return 0 if failed == 0 else 1


def test_gate_unconnected():
    """pytest 入口。"""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
