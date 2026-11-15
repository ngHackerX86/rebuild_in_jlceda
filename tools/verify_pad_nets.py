# -*- coding: utf-8 -*-
"""把转换结果里的焊盘网络与 KiCad 原设计**逐物理焊盘**对比。

对比对象分两层（对应复审建议的"生成声明核对"）：

1. **期望**：KiCad 每枚焊盘（按"位号 + 封装内序号"定位）的网络名，**空网络也算一项**；
2. **实际**：生成文件里的 `PAD_NET` 记录，id = `["PAD_NET", componentId, padNum, padId]`，
   用 padId 定位到封装里**那一枚**焊盘；没有记录就按"空网络"计。

因此"同号多焊盘、一枚有网一枚没有"会被如实比较，不会再假通过。

用法：
    python tools/verify_pad_nets.py <board.kicad_pcb> <工作目录或 .epcb2>

退出码：0 全部一致；2 有丢网络/不一致；3 只有"同号多焊盘分属不同网络"的结构性差异。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


# ------------------------------------------------------------------ KiCad 侧
def kicad_pads(board_path: str):
    """{(位号, 封装内序号): (焊盘号, 网络名)}"""
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    if board is None:
        raise SystemExit(f"无法读取板文件：{board_path}")
    out = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for idx, pad in enumerate(fp.Pads()):
            if not (pad.IsOnLayer(pcbnew.F_Cu) or pad.IsOnLayer(pcbnew.B_Cu)):
                continue        # 仅钢网/阻焊焊盘不生成 PAD，也不该参与网络比对
            out[(ref, idx)] = (str(pad.GetNumber()), pad.GetNetname() or "")
    return out


# ------------------------------------------------------------------ 生成侧
def _records(epcb2: Path):
    cur = None
    for line in epcb2.read_text(encoding="utf-8").splitlines():
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
        yield cur, h, d


def generated_pads(epcb2: Path):
    """返回 ({(位号, 封装内序号): (焊盘号, 网络名)}, 统计信息)。"""
    fp_pads: dict[str, list[str]] = {}
    pad_num: dict[str, str] = {}
    cur_title = None
    for cur, h, d in _records(epcb2):
        if cur != "FOOTPRINT":
            continue
        if h.get("type") == "META":
            cur_title = d.get("title")
            fp_pads.setdefault(cur_title, [])
            continue
        if h.get("type") == "PAD" and cur_title:
            pid = str(h.get("id") or "")
            if d.get("layerId") in (7, 8):
                continue
            pad_num[pid] = str(d.get("num") or "")
            fp_pads[cur_title].append(pid)

    comp: dict[str, tuple[str, str]] = {}
    for cur, h, d in _records(epcb2):
        if cur != "PCB" or h.get("type") != "COMPONENT":
            continue
        attrs = d.get("attrs") or {}
        ref = attrs.get("Unique ID") or ""
        try:
            title = json.loads(attrs.get("DeviceName") or "{}").get("name") or ""
        except Exception:
            title = ""
        comp[str(h.get("id") or "")] = (ref, title)

    # padId 只在**同一封装内**唯一，所以键必须是 (componentId, padId)
    net_by_pid: dict[tuple[str, str], str] = {}
    for cur, h, d in _records(epcb2):
        if cur != "PCB" or h.get("type") != "PAD_NET":
            continue
        try:
            key = json.loads(h["id"])
            cid, pid = str(key[1]), str(key[3])
        except Exception:
            continue
        if pid:
            net_by_pid[(cid, pid)] = d.get("padNet") or ""

    out = {}
    for cid, (ref, title) in comp.items():
        for idx, pid in enumerate(fp_pads.get(title, [])):
            out[(ref, idx)] = (pad_num.get(pid, ""), net_by_pid.get((cid, pid), ""))
    stats = {"components": len(comp), "pads": sum(len(v) for v in fp_pads.values()),
             "pad_nets": len(net_by_pid)}
    return out, stats


def find_epcb2(target: Path) -> Path:
    if target.is_file():
        return target
    hits = (sorted(target.glob("*_eprj3/pcb/*.epcb2")) or sorted(target.glob("pcb/*.epcb2"))
            or sorted(target.glob("**/*.epcb2")))
    if not hits:
        raise SystemExit(f"找不到 .epcb2：{target}")
    return hits[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="逐物理焊盘对比 KiCad 与转换结果的网络")
    ap.add_argument("board")
    ap.add_argument("project")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args(argv)

    epcb2 = find_epcb2(Path(a.project))
    exp = kicad_pads(a.board)
    gen, stats = generated_pads(epcb2)

    print(f"源板：{a.board}")
    print(f"生成：{epcb2}")
    print(f"器件 {stats['components']} 个 / 封装焊盘 {stats['pads']} 枚 / PAD_NET {stats['pad_nets']} 条")

    # 逐 "位号+焊盘号" 比较多重集合（含空网络，同号多焊盘一项都不能少）
    exp_ms = collections.defaultdict(collections.Counter)
    gen_ms = collections.defaultdict(collections.Counter)
    for (ref, _idx), (num, net) in exp.items():
        exp_ms[(ref, num)][net] += 1
    for (ref, _idx), (num, net) in gen.items():
        gen_ms[(ref, num)][net] += 1

    missing, mismatch, extra = [], [], []
    same_num_nets = {}
    for key, want in sorted(exp_ms.items()):
        got = gen_ms.get(key, collections.Counter())
        if sum(got.values()) < sum(want.values()):
            missing.append((key[0], key[1], sum(want.values()) - sum(got.values())))
        diff = {n: want[n] - got[n] for n in set(want) | set(got) if want[n] != got[n]}
        if diff:
            mismatch.append((key[0], key[1], dict(want), dict(got)))
        if sum(want.values()) > 1:
            same_num_nets[key] = sorted(want)
    for key in gen_ms:
        if key not in exp_ms:
            extra.append(key)

    def show(title, rows, fmt):
        print(f"\n**{title}**（{len(rows)}）")
        for r in rows[:a.limit]:
            print("  " + fmt(r))
        if len(rows) > a.limit:
            print(f"  … 其余 {len(rows) - a.limit} 条略")

    show("生成侧缺少的焊盘", missing, lambda r: f"{r[0]} pad {r[1]}: 缺 {r[2]} 枚")
    show("网络分布不一致（同号焊盘按多重集合比较）", mismatch,
         lambda r: f"{r[0]} pad {r[1]}: KiCad={r[2]} 生成={r[3]}")
    show("生成侧多出的焊盘组", extra, lambda r: f"{r[0]} pad {r[1]}")

    multi = {k: v for k, v in same_num_nets.items() if len(set(v)) > 1}
    if multi:
        print(f"\n**同号多焊盘且网络不同**（{len(multi)} 组，逐枚核对已通过）")
        for (ref, num), nets in list(multi.items())[:a.limit]:
            print(f"  {ref} pad {num}: {sorted(nets)}")

    print()
    if missing or mismatch:
        print(f"结论：存在连接性问题（缺焊盘 {len(missing)}、网络不一致 {len(mismatch)}）")
        return 2
    if multi:
        print(f"结论：逐枚核对一致；另有 {len(multi)} 组同号多焊盘分属不同网络（源设计如此）")
        return 3
    print("结论：逐枚焊盘网络与 KiCad 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
