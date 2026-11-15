# -*- coding: utf-8 -*-
"""把模板 `.epcb2` 最小化成"纯 boilerplate"：只留配置类记录，删掉具体设计内容。

背景：`rebuild_in_jlc/templates/PCB1.epcb2` 是从一个真实工程导出的，里面带着
30 个器件、397 条走线、4 块铺铜等**设计数据**。转换器其实只用到其中的配置类记录
（LAYER / LAYER_PHYS / RULE / PRIMITIVE …），其余都是随包分发的无关内容。

用法：
    python tools/minimize_template.py [-i 模板] [-o 输出]
    python tools/minimize_template.py --check 输出文件     # 只做"无实例记录"断言
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 转换器真正会用到的记录类型（与 kicad2eprj3.BOILER_TYPES 保持一致，外加文档头）
KEEP_TYPES = {
    "DOCHEAD", "META", "CANVAS",
    "PRIMITIVE", "LAYER", "LAYER_PHYS", "ACTIVE_LAYER", "RULE_TEMPLATE",
    "RULE", "SILK_OPTS", "D3_ATTRIBUTE", "PREFERENCE", "PANELIZE", "RULE_SELECTOR",
}
# 一旦出现就说明模板里还带着设计内容
DESIGN_TYPES = {"COMPONENT", "PAD", "LINE", "VIA", "POUR", "POURED", "POLY",
                "FILL", "STRING", "ATTR", "REGION", "NET", "PART", "ELE_PLACEHOLDER"}


def split_records(text: str):
    """按行切记录，返回 [(head_dict, body_str, raw_line)]。"""
    out = []
    for line in text.splitlines(True):
        head, sep, body = line.partition("}||")
        if not sep or not body.strip():
            continue
        try:
            h = json.loads(head + "}")
        except Exception:
            continue
        out.append((h, body.rstrip("\n")[:-1] if body.rstrip("\n").endswith("|") else body.rstrip("\n"),
                    line))
    return out


def minimize(src: Path, dst: Path) -> dict:
    text = src.read_text(encoding="utf-8")
    records = split_records(text)
    kept, dropped = [], {}
    ticket = 0
    for head, body, _raw in records:
        t = head.get("type")
        if t not in KEEP_TYPES:
            dropped[t] = dropped.get(t, 0) + 1
            continue
        h = dict(head)
        ticket += 1
        h["ticket"] = ticket if t != "DOCHEAD" else 0
        kept.append((h, body))

    lines = []
    for h, body in kept:
        lines.append(json.dumps(h, ensure_ascii=False, separators=(",", ":")) + "||" + body + "|")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"kept": len(kept), "dropped": dropped,
            "src_bytes": src.stat().st_size, "dst_bytes": dst.stat().st_size}


def check(path: Path) -> list[str]:
    """断言：不含任何实例/设计记录。返回问题列表（空 = 通过）。"""
    problems = []
    for head, _body, _raw in split_records(path.read_text(encoding="utf-8")):
        t = head.get("type")
        if t in DESIGN_TYPES:
            problems.append(f"残留设计记录：{t}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="最小化 .epcb2 模板")
    ap.add_argument("-i", "--input", default="rebuild_in_jlc/templates/PCB1.epcb2")
    ap.add_argument("-o", "--output", default="rebuild_in_jlc/templates/PCB1_min.epcb2")
    ap.add_argument("--check", default=None, help="只检查指定文件是否还有设计记录")
    a = ap.parse_args(argv)

    if a.check:
        problems = check(Path(a.check))
        if problems:
            print("✘ 模板仍含设计内容：" + "；".join(sorted(set(problems))))
            return 3
        print(f"✔ {a.check} 只有 boilerplate，无设计记录")
        return 0

    info = minimize(Path(a.input), Path(a.output))
    print(f"源模板 ：{a.input}（{info['src_bytes'] / 1024:.0f} KB，"
          f"{sum(info['dropped'].values()) + info['kept']} 条记录）")
    print(f"最小化 ：{a.output}（{info['dst_bytes'] / 1024:.0f} KB，{info['kept']} 条记录）")
    if info["dropped"]:
        top = sorted(info["dropped"].items(), key=lambda kv: -kv[1])[:8]
        print("删掉的记录：" + "、".join(f"{k}×{v}" for k, v in top))
    problems = check(Path(a.output))
    if problems:
        print("✘ 断言失败：" + "；".join(sorted(set(problems))))
        return 3
    print("✔ 断言通过：不含 COMPONENT/PAD/LINE/VIA/POUR/POURED/POLY/FILL/STRING/ATTR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
