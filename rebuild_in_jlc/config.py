# -*- coding: utf-8 -*-
"""插件配置：仓库根、EDA 可执行文件、KiCad 的 python 路径、模板路径。

优先级：环境变量 > 配置文件 > 自动探测。
配置文件默认在 `%APPDATA%/rebuild_in_jlc/config.json`（Windows）或 `~/.config/rebuild_in_jlc/config.json`。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
DEFAULT_TEMPLATE = PKG_DIR / "templates" / "PCB1_min.epcb2"   # 只含 boilerplate，见 tools/minimize_template.py


def _config_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return Path(base) / "rebuild_in_jlc" / "config.json"


def load() -> dict:
    """优先级：环境变量 > 配置文件 > 自动探测。

    （此前实现是先读环境变量再用配置文件 update 覆盖，与文档相反，已修正。）
    """
    cfg = {
        "repo_root": "",
        "eda_exe": "",
        "kicad_python": "",
        "template": "",
        "rules_profile": "kicad-min",
    }

    path = _config_path()
    if path.exists():
        try:
            cfg.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8-sig")).items()
                        if v not in ("", None)})
        except Exception:
            pass

    env_map = {
        "repo_root": ("REBUILD_IN_JLC_ROOT", "KICAD2JLC_ROOT"),
        "eda_exe": ("JLC_EDA_EXE",),
        "kicad_python": ("KICAD_PYTHON",),
        "template": ("REBUILD_IN_JLC_TEMPLATE", "KICAD2JLC_TEMPLATE"),
        "rules_profile": ("REBUILD_IN_JLC_RULES_PROFILE",),
    }
    for key, names in env_map.items():
        for name in names:
            value = os.environ.get(name)
            if value:
                cfg[key] = value
                break

    if not cfg["repo_root"]:
        cfg["repo_root"] = str(REPO_ROOT)
    if not cfg["template"]:
        cfg["template"] = str(DEFAULT_TEMPLATE)
    if not cfg["eda_exe"]:
        cfg["eda_exe"] = _guess_eda() or ""
    if not cfg["kicad_python"]:
        cfg["kicad_python"] = _guess_kicad_python() or ""
    return cfg


def save(cfg: dict) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _guess_eda() -> str | None:
    for p in (r"D:\Software\Electrical\lceda-proV4\lceda-pro.exe",
              r"C:\Program Files\lceda-pro\lceda-pro.exe",
              r"C:\Program Files (x86)\lceda-pro\lceda-pro.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("lceda-pro")


def _guess_kicad_python() -> str | None:
    """当前插件运行在 KiCad 的 python 里时，sys.executable 就是它。"""
    import sys
    exe = sys.executable or ""
    if exe and "python" in os.path.basename(exe).lower() and _has_pcbnew(exe):
        return exe
    for p in (r"D:\Software\Electrical\KiCAD10\bin\python.exe",
              r"C:\Program Files\KiCad\10.0\bin\python.exe",
              r"C:\Program Files\KiCad\9.0\bin\python.exe"):
        if os.path.exists(p):
            return p
    return None


def _has_pcbnew(python_exe: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([python_exe, "-c", "import pcbnew"], capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def check(cfg: dict) -> list[str]:
    """返回阻塞性问题列表（空 = 可以开始）。"""
    problems = []
    root = Path(cfg.get("repo_root") or "")
    if not (root / "tools" / "kicad2eprj3.py").exists():
        problems.append(f"仓库根目录不对（找不到 tools/kicad2eprj3.py）：{root}")
    if not Path(cfg.get("eda_exe") or "").exists():
        problems.append(f"嘉立创EDA 可执行文件不存在：{cfg.get('eda_exe')}")
    if not Path(cfg.get("template") or "").exists():
        problems.append(f"工程模板不存在：{cfg.get('template')}")
    return problems
