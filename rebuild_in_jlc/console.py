# -*- coding: utf-8 -*-
"""调试控制台：在 KiCad（GUI 进程）里也能看到运行过程。

KiCad 的 `pcbnew.exe` 是 GUI 程序，没有控制台，`print()` 的内容看不到。
这里按需分配一个控制台窗口（Windows 用 `AllocConsole`），并把 stdout/stderr 接上去，
之后 pipeline 的进度、每条子进程命令、退出码和输出尾巴都会打在这个窗口里。

开关：环境变量 `REBUILD_IN_JLC_VERBOSE=1` 时会把子进程**完整输出**都打出来；
默认只打命令行 + 退出码 + 最后几行。
"""
from __future__ import annotations

import os
import sys
import time

_console_ready = False


def ensure(title: str = "rebuild_in_jlc 运行日志") -> bool:
    """确保有一个可写的控制台；返回是否可用。"""
    global _console_ready
    if _console_ready:
        return True
    if os.name != "nt":
        _console_ready = True
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        if k32.GetConsoleWindow() == 0:          # 没有控制台就开一个
            if not k32.AllocConsole():
                return False
        try:
            k32.SetConsoleTitleW(title)
        except Exception:
            pass
        # 只有 GUI 进程（sys.stdout 为 None）才接管；否则保留原有重定向
        # （否则会把输出从管道/日志文件抢到控制台，命令行下的重定向就失效了）
        try:
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            if sys.stderr is None:
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        except Exception:
            pass
        _console_ready = True
        return True
    except Exception:
        return False


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str) -> None:
    ensure()
    print(f"[{_ts()}] {msg}", flush=True)


def step(title: str, detail: str = "") -> None:
    ensure()
    tail = f"  {detail}" if detail else ""
    print(f"[{_ts()}] === {title}{tail}", flush=True)


def cmd(argv: list[str]) -> None:
    ensure()
    print(f"[{_ts()}]   $ {' '.join(str(a) for a in argv)}", flush=True)


def result(code: int, elapsed: float, out: str, verbose: bool | None = None) -> None:
    ensure()
    if verbose is None:
        verbose = bool(os.environ.get("REBUILD_IN_JLC_VERBOSE"))
    print(f"[{_ts()}]   -> exit={code}  {elapsed:.1f}s", flush=True)
    text = (out or "").strip()
    if not text:
        return
    lines = text.splitlines()
    if not verbose and len(lines) > 12:
        shown = lines[:6] + ["   ..."] + lines[-6:]
    else:
        shown = lines
    for line in shown:
        print(f"[{_ts()}]   | {line}", flush=True)
