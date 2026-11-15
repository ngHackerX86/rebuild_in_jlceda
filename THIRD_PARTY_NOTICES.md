# 第三方来源与许可

本项目代码以 MIT 发布（见 `LICENSE`）。以下是仓库内**非本项目原创内容**的来源与授权说明。

## 1. 工程模板 `rebuild_in_jlc/templates/PCB1_min.epcb2`（当前分发文件）

- **用途**：生成嘉立创EDA 文件夹工程（`.eprj3`）时复用的 boilerplate（层表、层物理参数、规则模板等配置记录）。
- **来源**：由官方 KiCad → EasyEDA Pro 转换器的输出中提取。该转换器的许可为 **Apache-2.0**。
- **当前状态**：已用 `tools/minimize_template.py` 从原模板做**白名单提取**（只保留
  `LAYER / LAYER_PHYS / ACTIVE_LAYER / RULE / RULE_TEMPLATE / PRIMITIVE / SILK_OPTS /
  D3_ATTRIBUTE / PREFERENCE / PANELIZE / RULE_SELECTOR` 等配置类记录），并断言**不含**
  `COMPONENT / PAD / LINE / VIA / POUR / POURED / POLY / FILL / STRING / ATTR`。
  结果：530 KB / 1171 条 → 38 KB / 201 条；三块样例板用最小模板重建的记录数与之前一致。
- 复核方式：`python tools/minimize_template.py --check rebuild_in_jlc/templates/PCB1_min.epcb2`
  （或跑 `tests/offline/test_template_min.py`）。

## 2. 示例工程（不随仓库分发）

验证时使用过以下第三方开源工程，均**未提交进本仓库**，需要时请自行按其许可获取：

| 工程 | 用途 | 地址 | 许可 |
|---|---|---|---|
| HeyPCB ESP32-S3 DevKit | 四层主样例（1229 行记录） | <https://github.com/HeyPCB/esp32-s3-devkit> | MIT（见其仓库 `LICENSE`） |
| esp32c3-smart-board | 四层回归样例（2103 行） | 由 `tools/fetch_test_boards.py` 拉取的公开工程，commit 见脚本 | 以原仓库为准 |
| Ducky e-paper board | 两层回归样例（3171 行） | 同上 | 以原仓库为准 |
| MuffinByteLabs esp32s3-plant-monitor | 四层第三方样例（1720 行，DRC 0） | <https://github.com/MuffinByteLabs/esp32s3-plant-monitor> | 以原仓库为准（见其 `LICENSE`） |
| OpenAir Max | 早期备注样例（仅本地，未纳入回归） | 见其仓库自带 `LICENSE.txt` | 以原仓库为准 |

> 这些工程**均未提交进本仓库**，`scratch/` 与 `demo_*/` 已在 `.gitignore` 中排除；
> 需要时请按各自许可自行获取。仓库内只保存复现用的自造小板（`tests/fixtures/`）。

## 3. 格式规范与参考资料

| 资料 | 用途 | 许可/说明 |
|---|---|---|
| EasyEDA Pro 格式说明 <https://github.com/easyeda/easyeda-format-skill> | 字段语义、枚举与校验规则 | 以原仓库为准；本项目仅作参考，未随仓库分发其内容 |
| KiCad 官方文档（pcbnew / CLI） | API 与命令行行为 | CC BY 3.0 / GPL 视具体页面 |
| 嘉立创EDA 导入说明 | 导入行为对照 | 以官方站点为准 |

## 4. 生成物

`out/`、`release/` 等目录下的产物（`.eprj3`、`.epro2`、Gerber、报告）由使用者运行本工具生成，其内容归属取决于**输入板文件**的许可，本项目不对其主张权利。

## 5. 历史模板（不再分发）

早期版本随包分发 `PCB1.epcb2`（530 KB），它是从一个真实工程导出的，含具体设计数据
（30 个器件、397 条走线、4 块铺铜等）。0.4.0 起改用上面的最小模板，
旧模板仅作为历史说明保留在本仓库的旧提交中，**不随发布包分发**。
最小化方法见 `tools/minimize_template.py`（白名单 + "不得含实例记录"断言）。
