"""kicad_jlc_exporter.jlc —— 与 KiCad 解耦的核心库。

子模块：
  layers       层表与命名约定（KiCad 层 ↔ 嘉立创后缀）
  gerber       Gerber RS-274X 解析器
  excellon     Excellon 钻孔解析器
  preflight    文件级预检（纯 Python，可独立运行）
  board_rules  板级几何检查（需要 pcbnew）
  package     重命名 + 打包成嘉立创 zip
  flying_probe 生成 FlyingProbeTesting.json（需要 pcbnew）
  export_kicad 出图后端（pcbnew 或 kicad-cli）
  pipeline     端到端流水线
  cli          命令行入口
"""

__version__ = "0.5.0"
