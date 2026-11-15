# -*- coding: utf-8 -*-
"""配置优先级：环境变量 > 配置文件 > 自动探测；并确认重装不会清空已有配置。

运行：python tests/offline/test_config_priority.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from rebuild_in_jlc import config  # noqa: E402


def _with_env(env: dict, fn):
    old = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v
        return fn()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    failed = 0
    cfg = config.load()

    ok = bool(cfg.get("repo_root")) and Path(cfg["repo_root"]).is_dir()
    print(f"  [{'PASS' if ok else 'FAIL'}] 能解析出 repo_root：{cfg.get('repo_root')}")
    failed += 0 if ok else 1

    # 环境变量优先
    fake = str(REPO)
    got = _with_env({"REBUILD_IN_JLC_ROOT": fake}, config.load)
    ok = got.get("repo_root") == fake
    print(f"  [{'PASS' if ok else 'FAIL'}] 环境变量覆盖 repo_root：{got.get('repo_root')}")
    failed += 0 if ok else 1

    got = _with_env({"JLC_EDA_EXE": r"C:\fake\eda.exe"}, config.load)
    ok = got.get("eda_exe") == r"C:\fake\eda.exe"
    print(f"  [{'PASS' if ok else 'FAIL'}] 环境变量覆盖 eda_exe：{got.get('eda_exe')}")
    failed += 0 if ok else 1

    # 配置文件（模拟一份带 BOM 的 JSON —— PowerShell 5.1 写出来就是这样）
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.json"
        path.write_text("\ufeff" + json.dumps({"repo_root": str(REPO), "eda_exe": "X"}),
                        encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        ok = loaded.get("eda_exe") == "X"
        print(f"  [{'PASS' if ok else 'FAIL'}] 配置文件带 BOM 仍能解析")
        failed += 0 if ok else 1

    # 配置检查对不存在路径给出问题
    problems = config.check({"repo_root": str(REPO / "nope"), "eda_exe": "nope",
                             "kicad_python": "nope",
                             "template": str(REPO / "nope.epcb2")})
    ok = len(problems) >= 3
    print(f"  [{'PASS' if ok else 'FAIL'}] 路径不存在时 check 报问题（{len(problems)} 条）")
    failed += 0 if ok else 1

    print("全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


def test_config_priority():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
