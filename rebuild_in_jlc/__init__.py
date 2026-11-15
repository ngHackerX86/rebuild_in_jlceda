# -*- coding: utf-8 -*-
"""KiCad Action Plugin：把当前 PCB 重建到嘉立创EDA，跑 DRC 并导回 Gerber。

注册条件（三条同时满足才注册，避免在独立 python / CI 里触发 pcbnew 的 Pgm 断言）：
    1) `pcbnew` 已导入（说明在 KiCad 进程内）；
    2) `wx.GetApp()` 非空（说明 wx 应用已起来）；
    3) 没有设置 `REBUILD_IN_JLC_NO_REGISTER`（兼容旧名 `KICAD2JLC_NO_REGISTER`）（显式禁用开关，测试用）。

在纯 python 下本包仍可导入，`python -m rebuild_in_jlc.pipeline` 直接跑 headless 流水线。
"""
from __future__ import annotations

import os
import sys

__version__ = "0.4.0"


def _should_register() -> bool:
    if os.environ.get("REBUILD_IN_JLC_NO_REGISTER") or os.environ.get("KICAD2JLC_NO_REGISTER"):
        return False
    if "pcbnew" not in sys.modules:
        return False
    try:
        import wx

        return wx.GetApp() is not None
    except Exception:
        return False


if _should_register():
    try:
        from .plugin import register as _register  # noqa: F401

        _register()
    except Exception:                              # pragma: no cover
        pass
