"""kicad_jlc_exporter —— KiCad → 嘉立创（JLCPCB/嘉立创）出图插件。

本文件是 KiCad 插件入口：放在 plugins 目录下时 KiCad 会 import 本包并调用
register()。用命令行方式使用核心库时不需要 KiCad：

    python -m kicad_jlc_exporter.jlc.cli check  my_gerbers.zip
"""
from __future__ import annotations

__version__ = "0.5.0"

try:
    from .plugin import register, should_register

    # 只在 KiCad GUI 进程内注册：独立 Python 里注册会触发 C++ 断言并崩进程
    if should_register():
        register()
except Exception:                 # 没有 pcbnew/wx（例如在普通 Python 里跑 CLI）
    pass
