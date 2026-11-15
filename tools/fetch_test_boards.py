# -*- coding: utf-8 -*-
"""拉取用于回归测试的第三方 KiCad 工程（不随仓库分发）。

当前清单全部是 **KiCad 9** 设计的板子（文件 format 20241229），用来验证"旧版本工程能否
用新版 KiCad 的 pcbnew 读出并正确重建"。

用法：
    python tools/fetch_test_boards.py                # 拉到 scratch/samples/kicad9/
    python tools/fetch_test_boards.py --dir <目录>
    python tools/fetch_test_boards.py --list         # 只打印清单

注意：这些工程各有自己的许可，**不要提交进本仓库**；scratch/ 已在 .gitignore 中排除。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BOARDS = [
    {
        "name": "esp32c3-smart-board",
        "url": "https://github.com/petrovgp/esp32c3-smart-board",
        "commit": "5bbff2506fa6",
        "pcb": "kicad/esp32c3-smart-board.kicad_pcb",
        "desc": "KiCad 9 / 4 层 / 68 器件 / 263 焊盘 / 113 过孔 / 2 块铺铜（含内层）",
        "license": "仓库未附 LICENSE 文件，使用前请与作者确认",
    },
    {
        "name": "Ducky-board-ESP32-S3-with-1.52-inch-e-paper-display",
        "url": "https://github.com/MakersFunDuck/Ducky-board-ESP32-S3-with-1.52-inch-e-paper-display",
        "commit": "52e08f984854",
        "pcb": "hardware.kicad_pcb",
        "desc": "KiCad 9 / 2 层 / 98 器件 / 355 焊盘 / 356 过孔 / 1 块铺铜（跨两层）",
        "license": "见仓库 LICENSE",
    },
]


def fetch(target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    for b in BOARDS:
        dst = target / b["name"]
        if dst.exists():
            print(f"已存在，跳过：{b['name']}")
            continue
        print(f"克隆 {b['name']} ...")
        code = subprocess.call(["git", "clone", "--depth", "1", b["url"], str(dst)])
        if code != 0:
            print(f"  克隆失败（{code}）")
            continue
        pcb = dst / b["pcb"]
        print(f"  板文件：{'存在' if pcb.exists() else '缺失'}  {b['pcb']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="拉取第三方 KiCad 测试工程（不入库）")
    ap.add_argument("--dir", default="scratch/samples/kicad9", help="目标目录")
    ap.add_argument("--list", action="store_true", help="只打印清单")
    a = ap.parse_args(argv)

    if a.list:
        for b in BOARDS:
            print(f"{b['name']}\n  {b['url']}\n  commit={b['commit']}\n  {b['desc']}\n  许可：{b['license']}\n")
        return 0
    return fetch(Path(a.dir))


if __name__ == "__main__":
    sys.exit(main())
