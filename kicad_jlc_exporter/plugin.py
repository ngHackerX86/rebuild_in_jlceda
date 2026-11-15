"""KiCad ActionPlugin：菜单/工具栏一键出嘉立创 Gerber 包。

安装：把整个 kicad_jlc_exporter 目录放到
  Windows : %APPDATA%\\kicad\\<版本>\\plugins\\
  Linux   : ~/.local/share/kicad/<版本>/plugins/
  macOS   : ~/Library/Preferences/kicad/<版本>/plugins/
然后 PCB 编辑器 → 工具 → 外部插件 → 刷新。

没有 wx/pcbnew 时本模块只提供纯 Python 的 DEFAULTS，供 CLI 复用。
"""
from __future__ import annotations

import os

try:
    import pcbnew
    import wx
    _HAS_KICAD = True
except Exception:                                    # 纯命令行/无 GUI 环境
    pcbnew = None
    wx = None
    _HAS_KICAD = False


DEFAULTS = {
    "board_name": "",
    "outdir": "",
    "precision": 5,
    "separate_drills": True,
    "subtract_silk": True,
    "flying_probe": False,
}


def default_outdir(board_file: str) -> str:
    base = os.path.dirname(os.path.abspath(board_file)) if board_file else os.getcwd()
    return os.path.join(base, "jlc_fab")


# 注意：这里故意不提供"默认双层清单"。层清单必须由 pipeline.resolve_layers()
# 从板子的实际铜层数推导（评审 S-02：写死双面会让 4/6 层板漏内层）。


if _HAS_KICAD:

    class JLCDialog(wx.Dialog):
        """参数对话框：板名、输出目录、精度、三个关键开关。"""

        def __init__(self, parent, board_name: str, outdir: str):
            super().__init__(parent, title="导出嘉立创 Gerber 包",
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            sizer = wx.BoxSizer(wx.VERTICAL)
            grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=8)
            grid.AddGrowableCol(1, 1)

            grid.Add(wx.StaticText(self, label="板名（文件名前缀）"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.txt_name = wx.TextCtrl(self, value=board_name)
            grid.Add(self.txt_name, 1, wx.EXPAND)

            grid.Add(wx.StaticText(self, label="输出目录"), 0, wx.ALIGN_CENTER_VERTICAL)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.txt_dir = wx.TextCtrl(self, value=outdir)
            btn = wx.Button(self, label="浏览…", size=(70, -1))
            btn.Bind(wx.EVT_BUTTON, self._pick_dir)
            row.Add(self.txt_dir, 1, wx.EXPAND)
            row.Add(btn, 0, wx.LEFT, 6)
            grid.Add(row, 1, wx.EXPAND)

            grid.Add(wx.StaticText(self, label="Gerber 坐标精度"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.cmb_prec = wx.ComboBox(
                self, choices=["4.5（与嘉立创EDA一致）", "4.6（KiCad 默认）"],
                style=wx.CB_READONLY)
            self.cmb_prec.SetSelection(0)
            grid.Add(self.cmb_prec, 1, wx.EXPAND)

            sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 12)

            self.chk_drill = wx.CheckBox(self, label="PTH / NPTH 钻孔分成两个文件（推荐）")
            self.chk_drill.SetValue(True)
            self.chk_silk = wx.CheckBox(self, label="从丝印中挖掉阻焊开窗（避免丝印压焊盘）")
            self.chk_silk.SetValue(True)
            self.chk_fly = wx.CheckBox(self, label="同时生成飞针测试数据 FlyingProbeTesting.json")
            for c in (self.chk_drill, self.chk_silk, self.chk_fly):
                sizer.Add(c, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

            sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                      wx.EXPAND | wx.ALL, 12)
            self.SetSizerAndFit(sizer)

        def _pick_dir(self, _evt):
            dlg = wx.DirDialog(self, "选择输出目录", defaultPath=self.txt_dir.GetValue())
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_dir.SetValue(dlg.GetPath())
            dlg.Destroy()

        def collect(self) -> dict:
            return {
                "board_name": self.txt_name.GetValue().strip(),
                "outdir": self.txt_dir.GetValue().strip(),
                "precision": 5 if self.cmb_prec.GetSelection() == 0 else 6,
                "separate_drills": self.chk_drill.GetValue(),
                "subtract_silk": self.chk_silk.GetValue(),
                "flying_probe": self.chk_fly.GetValue(),
            }

    class JLCReportDialog(wx.Dialog):
        """预检结果 + 成品路径。"""

        def __init__(self, parent, text: str, zip_path: str | None):
            super().__init__(parent, title="嘉立创导出结果", size=(820, 560))
            sizer = wx.BoxSizer(wx.VERTICAL)
            mono = wx.TextCtrl(self, value=text,
                               style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
            try:
                mono.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))
            except Exception:
                pass
            sizer.Add(mono, 1, wx.EXPAND | wx.ALL, 10)
            row = wx.BoxSizer(wx.HORIZONTAL)
            if zip_path:
                b_open = wx.Button(self, label="打开输出目录")
                b_open.Bind(wx.EVT_BUTTON,
                            lambda e: _open_dir(os.path.dirname(zip_path)))
                row.Add(b_open, 0, wx.RIGHT, 8)
            row.Add(wx.Button(self, wx.ID_OK, "关闭"), 0)
            sizer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
            self.SetSizer(sizer)

    def _open_dir(path: str):
        try:
            if os.name == "nt":
                os.startfile(path)                    # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["open" if os.name == "darwin" else "xdg-open", path])
        except Exception:
            pass

    class JLCActionPlugin(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "导出嘉立创 Gerber 包…"
            self.category = "Fabrication"
            self.description = ("一键导出 Gerber/钻孔 → 预检（最小线宽/孔径/板框闭合/"
                                "包络越界/钻孔重复）→ 打包成嘉立创可直接上传的 zip")
            self.show_toolbar_button = True
            icon = os.path.join(os.path.dirname(__file__), "icon.png")
            self.icon_file_name = icon if os.path.isfile(icon) else ""

        def Run(self):
            from .jlc import pipeline

            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("请先打开一个 PCB", "嘉立创导出", wx.OK | wx.ICON_WARNING)
                return

            board_file = board.GetFileName()
            stem = (os.path.splitext(os.path.basename(board_file))[0]
                    if board_file else "PCB")

            dlg = JLCDialog(None, stem, default_outdir(board_file))
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                return
            opts = dlg.collect()
            dlg.Destroy()
            if not opts["board_name"]:
                opts["board_name"] = stem

            try:
                res = pipeline.run(
                    board_file, opts["outdir"], board_name=opts["board_name"],
                    layers=None,          # 由 pipeline 从实际铜层推导
                    merge_pth_npth=not opts["separate_drills"],
                    subtract_mask_from_silk=opts["subtract_silk"],
                    precision=opts["precision"],
                    with_flying_probe=opts["flying_probe"], board=board)
            except Exception as exc:                  # noqa: BLE001
                wx.MessageBox(f"导出失败：{exc}", "嘉立创导出", wx.OK | wx.ICON_ERROR)
                return

            text = res.to_text()
            if res.gate is not None and not res.gate.publishable:
                text = ("⚠ 发布判定未通过：本次产物不能直接投板。\n"
                        "  阻断项见下方 release gate。\n\n" + text)
            JLCReportDialog(None, text, res.zip_path).ShowModal()

    def should_register() -> bool:
        """只有在 KiCad GUI 进程内才注册。

        实测 KiCad 10.0.8：在独立 python.exe 里调用 ActionPlugin.register() 会触发
        C++ 断言 `PgmOrNull() failed in register_action()`，并把进程直接打挂
        （0x80000003），连 try/except 都拦不住。判据用 wx.GetApp()：
        GUI 进程里非 None，独立解释器里为 None。
        """
        try:
            return wx.GetApp() is not None
        except Exception:
            return False

    def register():
        if not should_register():
            return
        JLCActionPlugin().register()
