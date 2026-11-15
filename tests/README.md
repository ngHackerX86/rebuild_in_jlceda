# 回归测试

两类用例，**都不碰 EDA 账号**：

| 目录 | 运行环境 | 覆盖 |
|---|---|---|
| `tests/offline/` | 任意 Python 3.10+ | 配置优先级、DRC 三态、Gate 放行判定、对照工具五态、模板断言、发布清单 |
| `tests/kicad/` | 需要 `pcbnew`（KiCad 自带 python）| 无铺铜板转换、同封装多次钢网窗口、印膏扩展、层 ID 映射、矩形丝印/弧方向 |

## 怎么跑

```powershell
# 离线用例（任意 python）
python -m pytest tests/offline -q

# KiCad 环境用例（必须用带 pcbnew 的解释器）
& "D:\Software\Electrical\KiCAD10\bin\python.exe" -m pytest tests/kicad -q

# 只想手工看一眼：先造测试板，再逐个转换
& "D:\Software\Electrical\KiCAD10\bin\python.exe" tests\fixtures\make_fixtures.py --out out\fixtures
```

没有 pytest 时也可以直接跑脚本：`python tests/offline/test_compare_states.py` 之类
（每个文件都带 `if __name__ == "__main__"` 的自检入口）。

## 用例与缺陷对应

| 用例 | 对应 | 说明 |
|---|---|---|
| `offline/test_gate_unconnected.py` | F01 | 只有未连接项的 DRC 结果必须 `publishable=false` |
| `offline/test_compare_states.py` | F10 | 缺内铜参考 / 缺钻孔 / 整体偏移 / 正常两层 四种口径 |
| `offline/test_template_min.py` | 模板最小化 | 模板不得含设计记录，且能被子集断言通过 |
| `offline/test_release_manifest.py` | 发布清单 | `BUILD_INFO.json` 文件数/哈希与包内容一致，且不含 docx |
| `offline/test_config_priority.py` | 配置 | 环境变量 > 配置文件 > 自动探测 |





另有一个**手工**核对工具（需要 pcbnew，不放进自动用例）：

```powershell
& "...\KiCAD10\bin\python.exe" tools\verify_pad_nets.py <board.kicad_pcb> <工作目录或 .epcb2>
```

逐盘比较 KiCad 的 `pad.GetNetname()` 与生成文件里的 `PAD_NET` 记录，
报告「丢网络 / 网络不一致 / 同号多焊盘网络不同」三类问题。

## 待补（本轮明确未修）

复审 `F05 / F06 / F07 / F09` 对应的用例暂缺，修好后再补：

- `dry_run` 不应写入（F05）
- 重铺逐块记账 / 旧填充 / `--expect` 未达（F06）
- `rules`、`knockout` 失败必须阻断（F07）
- GUI 与 CLI 的 `run.json` 状态一致（F09）

## 约定

- 用例只用 `tests/fixtures/make_fixtures.py` 生成的**自造板**，不引用第三方工程或个人目录；
- 需要 EDA 的验证（导入、DRC、重铺、导出）**不放进自动测试**，按 `docs/开发与测试.md` 手工执行。

## 实际用例清单（0.4.0）

离线（不需要 pcbnew）：
- `offline/test_gate_unconnected.py` —— F01 放行判定（4 项）
- `offline/test_compare_states.py` —— F10 对照五态（4 项）
- `offline/test_polyline_simplify.py` —— N02 折线保形（5 项）
- `offline/test_template_min.py` —— 模板最小化（8 项）
- `offline/test_release_manifest.py` —— 发布清单（读实际构建产物）
- `offline/test_config_priority.py` —— 配置优先级（5 项）

KiCad 环境（需要 pcbnew）：
- `kicad/test_conversion_fixtures.py` —— 一个文件内含 7 项断言：
  F02 无铺铜、F03 同封装两次、F04 印膏扩展、圆丝印、N03 圆形钢网窗口、N01 PAD_NET padId、F08 改名内层
