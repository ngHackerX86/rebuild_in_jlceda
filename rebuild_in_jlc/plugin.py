# -*- coding: utf-8 -*-
"""KiCad Action Plugin：在 PCB 编辑器里放一个按钮，走完整条重建流水线。

设计约束（很重要）：
* 这个文件会被 KiCad 的 python 导入，**必须**在缺少 wx/pcbnew 时也能 import（供 CLI/CI 复用 pipeline）。
* 重活全部在 `pipeline.py` 里，通过子进程调用 `tools/` 下的脚本 —— 插件只做"参数收集 + 步骤展示 + 人工确认"。
* 「重建覆铜」只能人工，插件用一个明确的确认框把这一步显式化，不假装自动化。
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

try:                                    # 在 KiCad 里正常导入；在纯 python 下退化为不可用
    import pcbnew  # type: ignore
except Exception:                        # pragma: no cover
    pcbnew = None

try:
    import wx  # type: ignore
except Exception:                        # pragma: no cover
    wx = None

from . import config
from . import console, pipeline

PLUGIN_TITLE = "重建到嘉立创EDA（KiCad→JLCEDA）"


# --------------------------------------------------------------------------- 对话框
if wx is not None:

    class SettingsDialog(wx.Dialog):
        def __init__(self, parent, cfg: dict):
            super().__init__(parent, title="KiCad → 嘉立创EDA 设置")
            self.cfg = dict(cfg)
            grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
            self.fields = {}
            for key, label in (("repo_root", "仓库根目录"),
                               ("eda_exe", "嘉立创EDA 可执行文件"),
                               ("kicad_python", "KiCad python（含 pcbnew）"),
                               ("template", "工程模板 .epcb2")):
                grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                ctrl = wx.TextCtrl(self, value=str(self.cfg.get(key, "")), size=(520, -1))
                self.fields[key] = ctrl
                grid.Add(ctrl, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="规则档位"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.profile = wx.Choice(self, choices=["kicad", "kicad-min", "kicad-loose"])
            self.profile.SetStringSelection(self.cfg.get("rules_profile", "kicad-min"))
            grid.Add(self.profile, 0)

            ok = wx.Button(self, wx.ID_OK, "保存")
            cancel = wx.Button(self, wx.ID_CANCEL, "取消")
            btns = wx.BoxSizer(wx.HORIZONTAL)
            btns.AddStretchSpacer()
            btns.Add(ok, 0, wx.RIGHT, 8)
            btns.Add(cancel, 0)

            root = wx.BoxSizer(wx.VERTICAL)
            root.Add(grid, 1, wx.ALL | wx.EXPAND, 12)
            root.Add(btns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            self.SetSizerAndFit(root)

        def collected(self) -> dict:
            out = dict(self.cfg)
            for k, ctrl in self.fields.items():
                out[k] = ctrl.GetValue().strip()
            out["rules_profile"] = self.profile.GetStringSelection()
            return out

    class LogDialog(wx.Dialog):
        """跑完后的结果窗口：步骤状态 + 产物路径 + 失败日志。"""

        def __init__(self, parent, results: list, work: Path):
            super().__init__(parent, title="重建结果", size=(900, 620))
            root = wx.BoxSizer(wx.VERTICAL)
            text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            lines = [f"工作目录：{work}", ""]
            for r in results:
                mark = "✔" if r.get("ok") else "✘"
                lines.append(f"{mark} {r.get('title', r.get('step'))}")
                if r.get("uuid"):
                    lines.append(f"     工程 UUID：{r['uuid']}")
                if r.get("zip"):
                    lines.append(f"     Gerber 包：{r['zip']}")
                if r.get("report"):
                    lines.append(f"     对照报告：{r['report']}")
                if r.get("drc_passed") is not None:
                    lines.append(f"     DRC 通过：{r['drc_passed']}")
                if r.get("status") and r.get("step") == "repour":
                    lines.append(f"     覆铜状态：{r['status']}")
                if r.get("next"):
                    lines.append(f"     下一步：{r['next']}")
                if not r.get("ok"):
                    tail = (r.get("log") or "").strip().splitlines()[-12:]
                    lines.extend("       " + t for t in tail)
            text.SetValue("\n".join(lines))
            root.Add(text, 1, wx.ALL | wx.EXPAND, 10)
            btns = wx.BoxSizer(wx.HORIZONTAL)
            gerber_dir = self._gerber_dir(results, work)
            gerber_btn = wx.Button(self, label="打开Gerber目录")
            gerber_btn.Bind(wx.EVT_BUTTON, lambda _e: self._open(gerber_dir))
            gerber_btn.Enable(gerber_dir.exists())
            if not gerber_dir.exists():
                gerber_btn.SetToolTip("本次没有导出 Gerber（DRC 未通过时可能跳过）")
            open_btn = wx.Button(self, label="打开工作目录")
            open_btn.Bind(wx.EVT_BUTTON, lambda _e: self._open(work))
            close = wx.Button(self, wx.ID_CLOSE, "关闭")
            btns.Add(gerber_btn, 0, wx.RIGHT, 8)
            btns.Add(open_btn, 0, wx.RIGHT, 8)
            btns.AddStretchSpacer()
            btns.Add(close, 0)
            root.Add(btns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
            self.SetSizer(root)

        @staticmethod
        def _gerber_dir(results: list, work: Path) -> Path:
            """导出步骤给出的 zip 所在目录；没有就从结果里推断，最后退回 <工作目录>/gerber。"""
            for r in results:
                z = r.get("zip")
                if z:
                    p = Path(z)
                    return p.parent if p.is_file() or p.suffix else p
            return work / "gerber"

        @staticmethod
        def _open(path: Path):
            import subprocess
            import sys
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])

    class RepourDialog(wx.Dialog):
        """唯一必须人工的一步：编辑器不支持 API 触发重灌覆铜。"""

        def __init__(self, parent):
            super().__init__(parent, title="请手动重建覆铜")
            msg = (
                "工程已导入并打开在嘉立创EDA 中。\n\n"
                "请在 EDA 里执行一次： 设计 → 重建覆铜\n"
                "（本版本尚未实现自动重铺，这一步只能人工）\n\n"
                "灌完后回到这里点『已完成，继续』。\n"
                "注意：确认只代表你做了这一步，后续还要看 DRC 明细与导出物复核。"
            )
            root = wx.BoxSizer(wx.VERTICAL)
            root.Add(wx.StaticText(self, label=msg), 1, wx.ALL, 14)
            btns = wx.BoxSizer(wx.HORIZONTAL)
            done = wx.Button(self, wx.ID_OK, "已完成，继续")
            skip = wx.Button(self, wx.ID_CANCEL, "中止")
            btns.AddStretchSpacer()
            btns.Add(done, 0, wx.RIGHT, 8)
            btns.Add(skip, 0)
            root.Add(btns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
            self.SetSizerAndFit(root)


# --------------------------------------------------------------------------- 主流程
def _board_path() -> Path | None:
    if pcbnew is None:
        return None
    try:
        board = pcbnew.GetBoard()
        if board is None:
            return None
        path = board.GetFileName()
        return Path(path).resolve() if path else None
    except Exception:
        return None


def _run_with_ui(parent) -> None:
    console.ensure()                      # KiCad 是 GUI 进程，开一个控制台窗口看日志
    console.info(f"插件：{PLUGIN_TITLE}")
    board = _board_path()
    if board is None:
        wx.MessageBox("请先保存并打开一个 .kicad_pcb 文件。", PLUGIN_TITLE, wx.ICON_WARNING)
        return

    cfg = config.load()
    console.info(f"  板文件：{board}")
    console.info(f"  repo_root：{cfg.get('repo_root')}")
    console.info(f"  eda_exe  ：{cfg.get('eda_exe')}")
    console.info(f"  kicad_python：{cfg.get('kicad_python')}")
    problems = config.check(cfg)
    if problems:
        console.info(f"  配置问题：{problems}")
        if wx.MessageBox("配置有问题：\n\n" + "\n".join(problems) + "\n\n现在去设置？",
                         PLUGIN_TITLE, wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        dlg = SettingsDialog(parent, cfg)
        if dlg.ShowModal() != wx.ID_OK:
            return
        cfg = dlg.collected()
        config.save(cfg)
        problems = config.check(cfg)
        if problems:
            wx.MessageBox("仍然有问题：\n" + "\n".join(problems), PLUGIN_TITLE, wx.ICON_ERROR)
            return

    work = board.parent / "out" / f"rebuild_in_jlc_{__import__('time').strftime('%Y%m%d_%H%M%S')}"
    results: list = []

    def on_step(key, res):
        wx.CallAfter(wx.GetApp().Yield, True)

    dlg = RepourDialog(parent)

    def worker():
        try:
            console.step("开始重建", str(work))
            # 跑到第 5 步就停；随后先试自动重铺，失败才弹人工确认
            partial = pipeline.run(board, board.stem, work, stop_after="rules", on_step=on_step)
            results.extend(partial)
            if not partial or not partial[-1].get("ok"):
                wx.CallAfter(_finish, parent, results, work)
                return
            cfg = config.load()
            auto = pipeline.step_repour(cfg, auto=True)
            console.step(pipeline.STEP_TITLES["repour"], "自动重铺" if auto.get("ok") else "回退人工")
            auto["step"], auto["title"] = "repour", pipeline.STEP_TITLES["repour"]
            (work / "step_repour.log").write_text(auto.get("log") or "", encoding="utf-8")
            results.append(auto)
            if auto.get("ok"):
                wx.CallAfter(_continue_after_repour, parent, board, work, results)
                return
            wx.CallAfter(_ask_repour_then_continue, parent, dlg, board, work, results)
        except Exception:
            results.append({"step": "error", "title": "异常", "ok": False,
                            "log": traceback.format_exc()})
            wx.CallAfter(_finish, parent, results, work)

    threading.Thread(target=worker, daemon=True).start()


def _ask_repour_then_continue(parent, dlg, board: Path, work: Path, results: list) -> None:
    if dlg.ShowModal() != wx.ID_OK:
        results.append({"step": "repour", "title": "⑥ 重建覆铜", "ok": False,
                        "log": "用户中止"})
        _finish(parent, results, work)
        return

    def worker():
        try:
            cfg = config.load()
            manual = pipeline.step_repour(cfg, auto=False, manual_confirmed=True)
            manual["step"], manual["title"] = "repour", pipeline.STEP_TITLES["repour"]
            (work / "step_repour.log").write_text(manual.get("log") or "", encoding="utf-8")
            results.append(manual)
            rest = [pipeline.step_drc(cfg)]
            for key, res in (("drc", rest[0]),):
                res["step"] = key
                res["title"] = pipeline.STEP_TITLES[key]
                (work / f"step_{key}.log").write_text(res.get("log") or "", encoding="utf-8")
                results.append(res)
            drc = results[-1] if results else {}
            if drc.get("ok") and not drc.get("drc_passed"):
                answer: dict = {}
                done = threading.Event()

                def ask():
                    answer["go"] = wx.MessageBox(
                        "DRC 未通过（详见结果窗口）。\n\n是否仍然导出 Gerber 用于分析？\n"
                        "导出物不代表可用于投板。",
                        PLUGIN_TITLE, wx.YES_NO | wx.ICON_WARNING) == wx.YES
                    done.set()

                wx.CallAfter(ask)
                done.wait(timeout=300)
                if not answer.get("go", False):
                    results.append({"step": "export", "title": pipeline.STEP_TITLES["export"],
                                    "ok": False, "status": "skipped",
                                    "log": "用户选择不导出（DRC 未通过）"})
                    wx.CallAfter(_finish, parent, results, work)
                    return
            if results[-1].get("ok"):
                res = pipeline.step_export(cfg, work)
                res["step"], res["title"] = "export", pipeline.STEP_TITLES["export"]
                (work / "step_export.log").write_text(res.get("log") or "", encoding="utf-8")
                results.append(res)
                if res.get("ok"):
                    res2 = pipeline.step_compare(cfg, res["zip"], board, work)
                    res2["step"], res2["title"] = "compare", pipeline.STEP_TITLES["compare"]
                    (work / "step_compare.log").write_text(res2.get("log") or "", encoding="utf-8")
                    results.append(res2)
        except Exception:
            results.append({"step": "error", "title": "异常", "ok": False, "log": traceback.format_exc()})
        wx.CallAfter(_finish, parent, results, work)

    threading.Thread(target=worker, daemon=True).start()


def _continue_after_repour(parent, board: Path, work: Path, results: list) -> None:
    """自动重铺成功后继续：DRC →（未通过先问）→ 导出 → 对照。"""

    def worker():
        try:
            cfg = config.load()
            drc = pipeline.step_drc(cfg)
            drc["step"], drc["title"] = "drc", pipeline.STEP_TITLES["drc"]
            (work / "step_drc.log").write_text(drc.get("log") or "", encoding="utf-8")
            results.append(drc)
            if not drc.get("drc_passed"):
                answer: dict = {}
                done = threading.Event()

                def ask():
                    answer["go"] = wx.MessageBox(
                        "DRC 未通过（明细见结果窗口）。\n\n是否仍然导出 Gerber 用于分析？\n"
                        "导出物不代表可用于投板。",
                        PLUGIN_TITLE, wx.YES_NO | wx.ICON_WARNING) == wx.YES
                    done.set()

                wx.CallAfter(ask)
                done.wait(timeout=300)
                if not answer.get("go", False):
                    results.append({"step": "export", "title": pipeline.STEP_TITLES["export"],
                                    "ok": False, "status": "skipped",
                                    "log": "用户选择不导出（DRC 未通过）"})
                    wx.CallAfter(_finish, parent, results, work)
                    return
            res = pipeline.step_export(cfg, work)
            res["step"], res["title"] = "export", pipeline.STEP_TITLES["export"]
            (work / "step_export.log").write_text(res.get("log") or "", encoding="utf-8")
            results.append(res)
            if res.get("ok"):
                res2 = pipeline.step_compare(cfg, res["zip"], board, work)
                res2["step"], res2["title"] = "compare", pipeline.STEP_TITLES["compare"]
                (work / "step_compare.log").write_text(res2.get("log") or "", encoding="utf-8")
                results.append(res2)
        except Exception:
            results.append({"step": "error", "title": "异常", "ok": False,
                            "log": traceback.format_exc()})
        wx.CallAfter(_finish, parent, results, work)

    threading.Thread(target=worker, daemon=True).start()


def _finish(parent, results: list, work: Path) -> None:
    # 先在控制台打一条明显的结束信息，再弹结果窗口
    try:
        console.ensure()
        ok_n = sum(1 for r in results if r.get("ok"))
        done_all = bool(results) and results[-1].get("step") == "compare" and results[-1].get("ok")
        console.info("=" * 56)
        if done_all:
            console.info(f"✔ 重建成功：{ok_n}/{len(results)} 步完成（含导出与对照）")
        else:
            last = results[-1] if results else {}
            console.info(f"■ 重建结束：{ok_n}/{len(results)} 步完成，"
                         f"停在 {last.get('title', last.get('step', '-'))}")
            if last.get("next"):
                console.info(f"   说明：{last['next']}")
        drc = next((r for r in results if r.get("step") == "drc"), None)
        if drc is not None:
            console.info(f"   DRC：{'通过' if drc.get('drc_passed') else '未通过'}")
        console.info(f"   工作目录：{work}")
        console.info("=" * 56)
    except Exception:
        pass

    dlg = LogDialog(parent, results, work)
    dlg.ShowModal()
    dlg.Destroy()


# --------------------------------------------------------------------------- 注册
if pcbnew is not None and wx is not None:

    class JLCRebuildPlugin(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = PLUGIN_TITLE
            self.category = "制造 / 导出"
            self.description = "把当前 PCB 重建到嘉立创EDA，跑 DRC 并导回生产 Gerber 包"
            self.show_toolbar_button = True
            self.icon_file_name = ""

        def Run(self):
            _run_with_ui(wx.GetActiveWindow())

    def register():
        JLCRebuildPlugin().register()
