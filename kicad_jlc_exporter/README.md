# Gerber 检查与打包工具

这是仓库中的独立工具，用于检查已有 Gerber/钻孔文件、整理打包，或调用 KiCad 从 `.kicad_pcb` 出图。

若要迁移成**可编辑的嘉立创EDA 工程**，请使用 [主流程](../README.md)。

## 命令行

在源码根目录或完整发布包解压根目录运行。检查/打包已有文件使用普通 Python；从 PCB 出图需要 KiCad 环境。

```powershell
# 检查已有包或目录，生成报告
python -m kicad_jlc_exporter.jlc.cli check "gerber.zip" --out "report.txt"

# 整理并打包已有 Gerber 目录
python -m kicad_jlc_exporter.jlc.cli package "gerber" -o "Example_JLC.zip" --name Example

# 从 KiCad 板出图并打包
python -m kicad_jlc_exporter.jlc.cli export "C:\Boards\example.kicad_pcb" -o "out\Example" --name Example
```

当前 Python 不带 `pcbnew` 时，可在已安装 KiCad 且 `kicad-cli` 可用的环境中为 export 加 `--use-cli`。输入和输出路径请替换为自己的文件。

## KiCad 菜单

按 [安装指南](../docs/使用指南.md#安装) 安装 `kicad_jlc_exporter/`，刷新后选择 **工具 → 外部插件 → 导出嘉立创 Gerber 包…**，再选择输出目录。

## 看结果

查看检查报告、未执行项和输出路径。出现 `*_DIAGNOSTIC.zip` 表示诊断材料；普通命名的包也需要独立复核。

检查与打包不等于制造放行。实际工艺、层/孔/开窗和电气连通性需要另行确认。工具范围与验证记录见 [支持范围与限制](../docs/支持范围与限制.md)、[验证记录](../docs/验证记录.md)。
