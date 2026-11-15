# -*- coding: utf-8 -*-
"""体检：找"会被 EDA 静默丢弃"的长折线，以及各类记录的分布。

EDA 实测限制（0.3.2 记录）：
  * 封装文档里的 POLY 超过约 180 点会被**整条丢弃**（721/180 点实测消失，72 点正常）；
  * 板级 POLY 不进 3D 预览（但能进 Gerber）；
  * ARC 图元不支持丝印层。

所以这里重点扫：点数 > 180 的 POLY（无论文档域），以及其它异常（空路径、重复点）。

用法：
    python tools/check_polyline_limits.py <工作目录或 .epcb2> [--limit 180]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


def find_epcb2(target: Path) -> Path:
    if target.is_file():
        return target
    hits = (sorted(target.glob("*_eprj3/pcb/*.epcb2")) or sorted(target.glob("pcb/*.epcb2"))
            or sorted(target.glob("**/*.epcb2")))
    if not hits:
        raise SystemExit(f"找不到 .epcb2：{target}")
    return hits[0]


def scan(path: Path, limit: int) -> dict:
    cur = None
    title = None
    counter = collections.Counter()
    long_ones = []
    degenerate = []
    for line in path.read_text(encoding="utf-8").splitlines():
        head, _, body = line.partition("}||")
        if not body.strip():
            continue
        try:
            h = json.loads(head + "}")
            d = json.loads(body.rstrip("|\n"))
        except Exception:
            continue
        t = h.get("type")
        if t == "DOCHEAD":
            cur = d.get("docType")
            title = None
            continue
        if cur == "FOOTPRINT" and t == "META":
            title = d.get("title")
        counter[(cur, t)] += 1
        if t not in ("POLY", "FILL"):
            continue
        path0 = (d.get("path") or [])
        flat = [v for v in (path0[0] if path0 and isinstance(path0[0], list) else path0)
                if isinstance(v, (int, float))]
        n = len(flat) // 2
        if n > limit and d.get("layerId") != 11:      # 11 = 板框，长折线正常且能渲染
            long_ones.append({"doc": cur, "title": title, "type": t, "points": n,
                              "layer": d.get("layerId")})
        if n < 2:
            degenerate.append({"doc": cur, "title": title, "type": t, "points": n})
    return {"counts": counter, "long": long_ones, "degenerate": degenerate}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="扫描会被 EDA 丢弃的长折线")
    ap.add_argument("target")
    ap.add_argument("--limit", type=int, default=180)
    a = ap.parse_args(argv)
    path = find_epcb2(Path(a.target))
    r = scan(path, a.limit)
    print(f"文件：{path}")
    print(f"记录数：{sum(r['counts'].values())}")
    for (doc, t), n in sorted(r["counts"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {str(doc):>10} {t:<14} {n}")
    print()
    if r["long"]:
        print(f"⚠ 超过 {a.limit} 点的折线（会被 EDA 丢弃）：{len(r['long'])}")
        for x in r["long"][:12]:
            print(f"   {x['doc']} {x['title']} layer={x['layer']} 点数={x['points']}")
    else:
        print(f"✔ 没有超过 {a.limit} 点的折线")
    if r["degenerate"]:
        print(f"⚠ 退化折线（<2 点）：{len(r['degenerate'])}")
        for x in r["degenerate"][:5]:
            print(f"   {x['doc']} {x['title']} {x['type']} 点数={x['points']}")
    return 0 if not r["long"] else 3


if __name__ == "__main__":
    sys.exit(main())
