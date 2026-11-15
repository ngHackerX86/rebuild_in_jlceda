# 变更记录

格式参考 Keep a Changelog；版本号跟随 `rebuild_in_jlc/metadata.json`。

## [0.4.0] - 2026-11-15

按第二轮续审（N01–N03 / N06 + 状态骨架）修复。**注意：本轮修正了 0.3.3 的两处过度表述**
（"logo 不再丢""保形简化"不成立，见下）。

### 修复

- **N01 同号焊盘网络被覆盖**：`PAD_NET` 现在带 **padId**（`["PAD_NET", componentId, padNum, padId]`），
  指向封装内**那一枚**焊盘。实测：同号两枚焊盘一枚 `SIGNAL`、一枚空网络，导入后回读分别是
  `SIGNAL` / 空 —— 0.3.3 之前会两枚都变 `SIGNAL`。缺少 padId 时转换**直接失败**，不再静默覆盖。
- **N01 验证器假通过**：`tools/verify_pad_nets.py` 改为**逐物理焊盘**比较（按位号+焊盘号做多重集合，
  **空网络也参与**），并区分"生成侧缺焊盘/网络分布不一致/同号多网"。C3 得到 exit 3（`J1.S1` 四种
  `unconnected-*`），Ducky / 主样例 / plant-monitor 为 exit 0。
- **N02 折线简化截断**（0.3.3 引入的退化）：取消等距预抽点与 `[:limit]` 截断，改为**误差受控**简化
  （保首末点、保闭合、ε 逐级放大，达不到就标记降级并写 `degraded_polylines.json`，**绝不牺牲几何**）。
  同时纠正前提：实测同一封装内 80…2000 点的多边形**全部正常渲染**，"180 点上限"的旧结论不成立，
  因此默认不再简化（上限默认 2000，可用 `JLC_SILK_POLY_LIMIT` 调整）。
- **N03 钢网窗口形状失真**：仅钢网焊盘的窗口改为按**焊盘真实形状**生成（圆用 64 边形、
  其余用 `GetEffectivePolygon`）。直径 1 mm 圆垫的窗口面积从 1.0 mm²（+27.3%）变为 **0.7841 mm²**
  （理想 0.7854，−0.17%）。
- **N06 发布清单**：`file_count` 计入 install.ps1 / VERSION.json / BUILD_INFO；
  install.ps1 与 VERSION.json 纳入逐文件 SHA256；**没有 Git 元数据时直接失败**（不再产出三文件"成功包"）；
  构建后用 ZIP 实际内容复核文件集合与数量。
- **状态骨架**：`knockout` / `rules` 失败默认**阻断**后续步骤，新增 `--continue-on-error`
  仅供诊断使用。

### 明确未做（下轮）

F05（dry-run 仍写入）、F06（重铺逐块记账）、F09（GUI/CLI 状态统一）、N04（六层/空目标/单位）、
N07（旧模板路径升级迁移）。

## [0.3.3] - 2026-11-15

### 修复

- **长折线会被 EDA 静默丢弃**：除圆以外，封装内的 logo 多边形（Ducky 502 点、plant 360 点）
  同样会整条消失。新增 `limit_polyline()`（等距抽稀 + Douglas-Peucker 逐级放宽，上限 150 点，
  可用 `JLC_SILK_POLY_LIMIT` 调整），丝印层的封装多边形、离散弧、板级折线统一限点。
- 新增体检工具 `tools/check_polyline_limits.py`：扫出会被丢弃的长折线（板框层 11 除外），
  四块板现在都是"无超限折线"。

### 实测

- Ducky：B.Silk 里 502 点的 logo 恢复；B_Silkscreen 包围盒偏差从（此前缺 logo）收敛到 0.57mm。
- 四块板记录数：C3 2103 / Ducky 3171 / 主样例 1229 / plant 1720；焊盘网络逐盘一致
  （C3 仅 `J1.S1` 同号多焊盘，原设计即如此）。

## [0.3.2] - 2026-11-15

### 修复

- **测试点外圈（封装内整圆丝印）在 3D 预览里仍然不见**。逐项实测后定位为三条 EDA 限制：
  1. 封装文档里的 `POLY` **点数过多会被丢弃**——721 点、180 点整圈消失，24 点、72 点正常
     （同板的 360 点 logo 是多边形 `SHAPE_T_POLY`，走另一条分支，能正常渲染）；
  2. **板级** `POLY` 能进 Gerber，但不进 3D 预览（0.3.1 的做法，导出正确、画面缺失）；
  3. `ARC` 图元**不支持丝印层**：`pcb_PrimitiveArc.create` 在 layer=3 报参数错误，铜层正常。
  结论：圆丝印写回**封装文档**，段数默认 **72（5°）**，可用 `JLC_CIRCLE_SEGMENTS` 调整。
- 封装内的**弧**离散步进也从 0.5° 放宽到 5°，避免点数超限被丢弃。

### 实测

- plant-monitor：F.Silk Gerber 12 308 → **13 604 段**，TP5 圆环 73 个点；`F_Silkscreen` 判 `compared`。
- 三块老板记录数：C3 2103、Ducky 3171、主样例 1229（不变），DRC 无退化。

## [0.3.1] - 2026-11-15

### 修复

- **封装内的整圆丝印整圈丢失**（测试点外圈）：`circle_points()` 用 `|GetStart() - GetCenter()|` 算半径，
  而 KiCad 的 `PCB_SHAPE` 圆把 **GetStart() 存成圆心**、`GetEnd()` 才是圆周上一点 → 半径恒为 0，
  圆被静默丢弃。改为 `GetRadius() → GetEnd() → 包围盒` 依次兜底。
  另外封装文档里的长折线 POLY 在 EDA 里不渲染，圆改为**按实例发板级 POLY**。
  plant-monitor 实测：F.Silk Gerber 从 319 613 字节 / 12 308 段 → 346 818 字节 / 13 388 段（+6 个圆环）。
- 三块老板记录数相应 +1（C3 2103、Ducky 3171、主样例 1229 不变），DRC 无退化。

## [0.3.0] - 2026-11-15

按外部复审意见（`F01–F04`、`F08`、`F10`、`F11`）修复数据完整性与对照口径问题，并改造发布流程。
**状态：开发预览。** 仍不提供制造放行判定。

### 修复（数据完整性）

- **F01 Gerber 插件漏算未连接项**：`run_drc()` 现在把 `unconnected_items` 作为结构化字段返回；
  Gate 新增「DRC 连通性」阶段，未连接 > 0 或 DRC 报告解析失败一律阻断，
  只产出 `*_DIAGNOSTIC.zip`。此前"只有未连接"的板会被判为 `publishable=true`。
- **F02 无铺铜板崩溃**：`fill_items` / `pour_id` 提前到铺铜循环外初始化，`SKIP_POURED` 时不再构造填充；
  无 POUR 的板转换正常通过，自动重铺返回 `not_needed`（正常状态）而不是失败。
- **F03 同封装多次放置漏钢网窗口**：独立印膏窗口改为**按器件实例**生成（原先只在去重后的封装库定义里生成一次）。
  两实例的 1×1 mm F.Paste 板上，板级 FILL 从 1 个恢复为 2 个。
- **F04 印膏扩展被静默吞掉**：`_exp_mil()` 支持 KiCad 10 的 `VECTOR2I` 返回值（两轴不同取较大者并告警）；
  `fp_signature()` 纳入阻焊/钢网扩展、圆角比例与图元线宽，避免"只有扩展不同"的实例被合并。
  0.1 / 0.3 mm 的源值现在写成 3.937 / 11.811 mil，不再是 `null`。

### 修复（层映射与对照口径）

- **F08 重命名内层错位**：层映射从"按显示名"改为**按 KiCad 层 ID + 启用叠层顺序**（`LayerMapper`）。
  把 `In1.Cu` 改名成 `PWR` 后走线落到 EDA 内层 1（15），不再掉进文档层 13。
  无法映射的层**默认阻断**转换并列出受影响对象，`--allow-unknown-layer` 才继续。
- **对照工具两处误判（由 plant-monitor 板暴露）**：内层参考改为按 `.g<序号>` 扩展名匹配
  （源板把内层改名成 `GND1_Cu`/`GND2_Cu` 时不再报"缺证据"）；钻孔刀具按**数值**比较
  （EDA 写 `0.20000`、KiCad 写 `0.200` 不再误判差异）。
- **F10 对照工具口径**：新增 `--source-copper N` 与 `--json`，每层输出
  `compared / differs / missing_evidence / parse_error / not_applicable` 五态；
  缺内铜参考不再当"两层板正常"，缺钻孔参考不再静默跳过，包围盒偏移 > 0.5 mm 判 `differs`。
  退出码：0 全比较、3 缺证据/解析失败、4 存在偏移差异。
- **F11 停止点与错误指引**：补上 `--stop-after knockout` 停止分支；删掉
  "重跑 + `--repour-confirmed` 继续"的错误说明（重跑会导入新工程，不能衔接上次停止点）。

### 变更（发布）

- **模板最小化**：默认模板改为 `templates/PCB1_min.epcb2`（38 KB / 201 条），
  由 `tools/minimize_template.py` 从原 530 KB 模板白名单裁剪，并断言不含
  `COMPONENT/PAD/LINE/VIA/POUR/POURED/POLY/FILL/STRING/ATTR`。
  三块测试板用最小模板重建的记录数与之前一致（2102 / 3170 / 1229），EDA 导入正常（250 焊盘 / 68 器件 / 846 线）。
- **发布清单固定**：`scripts/build_release.py` 只打包 **git 跟踪**的文件，产出 `BUILD_INFO.json`
  （源 commit、是否 dirty、逐文件 SHA256 与大小）。此前发布包会混入本地 `*.docx`。
- **install.ps1 兼容 PowerShell 5.1**：去掉 `ConvertFrom-Json -AsHashtable`；
  读取旧配置失败时**不再覆盖**；插件目录按 `%APPDATA%` / `Documents` 探测，可用 `-PluginsDir` 指定。
- 版本 0.3.0；两份 `metadata.json` 的占位作者/网址已填。

### 新增

- `tests/`（离线 + KiCad 环境两类回归用例）与 `docs/开发与测试.md`。
- `tools/minimize_template.py`、`CHANGELOG.md`。

### 已知问题（本轮未修，见复审 F05–F07、F09）

- `--dry-run` 仍会真正执行重铺并保存（探测在调用之后才判断）；
- 自动重铺只要"有一块灌上了"就报成功，逐块失败/旧填充/未达 `--expect` 不区分；
- `rules` / `knockout` 步骤失败不会阻断后续步骤；GUI 后半程不写 `run.json`；
- 对照工具目前只做统计与完整性判定，不做几何等价证明。
