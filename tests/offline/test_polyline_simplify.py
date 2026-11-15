# -*- coding: utf-8 -*-
"""N02：折线简化必须保形（不丢尖峰、不截尾、不破闭合）。

不需要 pcbnew：直接调用 `tools/kicad2eprj3.py` 里的纯函数。
运行：python tests/offline/test_polyline_simplify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import kicad2eprj3 as k  # noqa: E402


def case_peak() -> bool:
    """2000 点直线 + 第 7 点尖峰：预抽点会把它抽掉。"""
    pts = [(i * 10.0, 0.0) for i in range(2000)]
    pts[7] = (70.0, 100.0)
    out, info = k.simplify_polyline(pts, limit=150)
    peak = max(y for _x, y in out)
    ok = peak >= 90 and out[0] == pts[0] and out[-1] == pts[-1]
    print(f"  [{'PASS' if ok else 'FAIL'}] 尖峰保留：{len(out)} 点 maxY={peak:.1f}"
          f"（dev={info and info['max_dev_mm']}mm）")
    return ok


def case_endpoint() -> bool:
    """400 点锯齿：旧实现会 `[:150]` 截断，末端 X 从 39900 掉到 14900。"""
    pts = [(i * 100.0, 50.0 if i % 2 else 0.0) for i in range(400)]
    out, info = k.simplify_polyline(pts, limit=150)
    ok = out[-1][0] == 39900 and out[0][0] == 0
    degraded = bool(info and info.get("degraded"))
    print(f"  [{'PASS' if ok else 'FAIL'}] 端点不丢：{len(out)} 点 末端X={out[-1][0]:.0f}"
          f"（降级标记={degraded}）")
    return ok


def case_closed() -> bool:
    """闭合图形：末点必须仍等于首点。"""
    pts = [(i * 100.0, 50.0 if i % 2 else 0.0) for i in range(402)] + [(0.0, 0.0)]
    out, _info = k.simplify_polyline(pts, limit=150, closed=True)
    ok = tuple(out[-1]) == (0.0, 0.0)
    print(f"  [{'PASS' if ok else 'FAIL'}] 闭合性：{len(out)} 点 末点={out[-1]}")
    return ok


def case_error_bounded() -> bool:
    """可简化图形：点数达标且最大偏差 <= ε。"""
    import math
    pts = [(100 * math.cos(2 * math.pi * i / 720), 100 * math.sin(2 * math.pi * i / 720))
           for i in range(720)]
    pts.append(pts[0])
    out, info = k.simplify_polyline(pts, limit=64, closed=True)
    ok = info is not None and not info["degraded"] and len(out) <= 64 \
        and info["max_dev_mm"] <= k.SILK_SIMPLIFY_EPS_MM + 1e-9
    print(f"  [{'PASS' if ok else 'FAIL'}] 误差受控：{len(out)} 点 "
          f"dev={info and info['max_dev_mm']}mm（上限 {k.SILK_SIMPLIFY_EPS_MM}mm）")
    return ok


def case_no_limit() -> bool:
    """默认上限下（2000 点）不应对正常图形做任何改动。"""
    pts = [(float(i), float(i % 7)) for i in range(500)]
    out, info = k.simplify_polyline(pts)
    ok = out == pts and info is None
    print(f"  [{'PASS' if ok else 'FAIL'}] 默认不改动（{len(out)} 点，info={info}）")
    return ok


def main() -> int:
    results = [case_peak(), case_endpoint(), case_closed(), case_error_bounded(), case_no_limit()]
    ok = all(results)
    print("全部通过" if ok else f"{results.count(False)} 项失败")
    return 0 if ok else 1


def test_polyline_simplify():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
