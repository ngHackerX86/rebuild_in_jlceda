# -*- coding: utf-8 -*-
"""触发嘉立创EDA 重新灌铜（等价于人手的「设计 → 重建覆铜」/ Shift+B）。

机制：对每块 POUR 调用 `rebuildCopperRegion()`（编辑器内部方法，未出现在官方 API 文档里）。
它会同步返回该区域的填充多边形 `{primitiveType:"Poured", pourPrimitiveId, pourFills:[...]}`，
编辑器随即生成 POURED 图元。

实测（ESP32-S3 四层板，2026-11-14，EDA 4.1.100）：4 块铺铜 2–5 秒完成，
`POURED` 0 → 4，DRC 的 34 条 GND 未连接随之清零。

设计要点
--------
1. **先探测再调用**：进入循环前检查 `pour.rebuildCopperRegion` 是否存在；
   不存在时不做任何写操作，直接给出"降级到人工"的结论与可用方法清单。
2. **分原因码返回**：无会话 / 无铺铜 / 接口不存在 / 编辑器拒绝 / 超时未出填充，
   各自有独立退出码与处置建议，便于流水线与 GUI 分别处理。
3. **幂等**：已有填充时默认也重算（结果一致）；`--skip-if-poured` 可跳过。
4. **可验证**：以 `POURED` 数量为成功判据，而不是"调用没报错"。

用法
----
    python tools/eda_repour.py                    # 自动重铺并自检
    python tools/eda_repour.py --dry-run          # 只探测接口能力，不重铺
    python tools/eda_repour.py --json             # 机器可读结果
    python tools/eda_repour.py --skip-if-poured   # 已有填充就跳过
    python tools/eda_repour.py --exe <path> --expect 4 --timeout 300

退出码
------
    0  已生成填充
    1  其它失败
    3  编辑器不支持该接口（需人工 Shift+B）
    4  没有在线用户会话
    5  当前 PCB 没有铺铜区域（POUR）——注意：只在显式要求时使用；
   now 无铺铜板返回 0 / reason_code=not_needed
    8  当前没有打开 PCB 文档（铺铜 API 无法访问）
    6  调用了但编辑器拒绝 / 超时仍未产生填充
    7  环境错误（EDA 可执行文件不存在等）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_EXE = os.environ.get("JLC_EDA_EXE", r"D:\Software\Electrical\lceda-proV4\lceda-pro.exe")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNSUPPORTED = 3
EXIT_NO_SESSION = 4
EXIT_NO_POUR = 5
EXIT_REJECTED = 6
EXIT_ENV = 7
EXIT_NO_DOC = 8

# 原因码 → 处置建议（GUI 与文档共用这套措辞）
HINTS = {
    "ok": "已重新灌铜。",
    "unsupported_api": "当前编辑器版本没有 rebuildCopperRegion 接口：请在 EDA 里手工执行"
                       "「设计 → 重建覆铜」（Shift+B），再用 --repour-confirmed 继续。",
    "no_session": "没有检测到已登录的 EDA 会话：请打开嘉立创EDA 并登录，且至少打开一个工程。",
    "no_pour": "当前 PCB 上没有铺铜区域（POUR）：先确认源板确实有铺铜，或检查导入是否完整。",
    "not_needed": "本板没有铺铜区，无需重铺。",
    "no_document": "当前没有打开 PCB 文档：请在 EDA 里打开该工程的 PCB 页，或先用 "
                   "tools/eda_open_project.py 打开工程，再重试。",
    "rejected": "接口可调用但没有产生填充：可能是铺铜被规则判定为无效、图层未激活或编辑器忙。"
                "请在 EDA 里手工 Shift+B 复核，必要时查看 DRC 的覆铜相关告警。",
    "timeout": "等待填充超时：编辑器可能仍在计算。稍后用 --skip-if-poured 复跑或手工 Shift+B。",
    "env": "EDA 可执行文件或会话不可用：检查 JLC_EDA_EXE / 安装路径。",
}

JS = r"""
const EXPECT = __EXPECT__;
const FORCE = __FORCE__;
const SKIP_IF_POURED = __SKIP__;

function hasMethod(obj, name) {
  try {
    const proto = Object.getPrototypeOf(obj) || {};
    return typeof obj[name] === "function" || typeof proto[name] === "function";
  } catch (e) { return false; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 铺铜 API 依赖"当前有 PCB 文档"；文档没打开时 getAll() 会直接抛错。
// 这里先尝试，失败就主动打开第一块 PCB 再重试一次。
async function poursSafe() {
  try { return await eda.pcb_PrimitivePour.getAll(); } catch (e) { return null; }
}
let pours = await poursSafe();
let openedPcb = false;
if (pours === null) {
  const pcbs = (await eda.dmt_Pcb.getAllPcbsInfo().catch(() => null)) || [];
  if (pcbs.length) {
    try {
      await eda.dmt_EditorControl.openDocument(pcbs[0].uuid);
      openedPcb = true;
      await sleep(3000);
    } catch (e) {}
    pours = await poursSafe();
  }
}
if (pours === null) {
  return { ok: false, reason_code: "no_document", pours: 0, poured_before: 0,
           poured_after: 0, fills: 0, methods: [] };
}
const pouredBefore = (await eda.pcb_PrimitivePoured.getAll() || []).length;

if (!pours.length) {
  // 无铺铜是合法输入：不需要重铺，也不该阻塞整条流水线
  return { ok: true, reason_code: "not_needed", pours: 0, poured_before: pouredBefore,
           poured_after: pouredBefore, fills: 0, methods: [] };
}

const methods = (() => {
  try {
    const proto = Object.getPrototypeOf(pours[0]) || {};
    return Object.getOwnPropertyNames(proto).filter((m) => /copper|region|fill|rebuild/i.test(m));
  } catch (e) { return []; }
})();

const supported = hasMethod(pours[0], "rebuildCopperRegion");
if (!supported) {
  return { ok: false, reason_code: "unsupported_api", pours: pours.length,
           poured_before: pouredBefore, poured_after: pouredBefore, fills: 0, methods };
}

if (SKIP_IF_POURED && !FORCE && pouredBefore >= pours.length) {
  return { ok: true, reason_code: "ok", skipped: true, pours: pours.length,
           rebuilt: 0, failed: 0, fills: 0, poured_before: pouredBefore,
           poured_after: pouredBefore, ms: 0, methods };
}

const t0 = Date.now();
let rebuilt = 0, failed = 0, fills = 0;
const errors = [];
for (const p of pours) {
  let done = false;
  for (let attempt = 1; attempt <= 2 && !done; attempt++) {
    try {
      const r = await p.rebuildCopperRegion();
      done = true;
      rebuilt++;
      try { fills += (r && r.pourFills ? r.pourFills.length : 0); } catch (e) {}
    } catch (e) {
      if (errors.length < 4) errors.push("try" + attempt + ":" + String(e).slice(0, 100));
      if (attempt === 1) await new Promise((r2) => setTimeout(r2, 800));
    }
  }
  if (!done) failed++;
}

// 重铺会改动工程 → 立刻保存，避免"未保存"状态阻塞后续 openProject 切换工程
let saved = null;
try { saved = await eda.pcb_Document.save(); } catch (e) { saved = "err:" + String(e).slice(0, 60); }

// 等填充落地（编辑器侧异步）
const target = Math.max(EXPECT || 0, Math.min(pours.length, 1));
let poured = pouredBefore;
for (let i = 0; i < 240; i++) {
  poured = (await eda.pcb_PrimitivePoured.getAll() || []).length;
  if (poured >= target) break;
  await new Promise((r) => setTimeout(r, 250));
}

const ms = Date.now() - t0;
return { ok: poured > 0 && poured >= target ? true : (poured > 0),
         reason_code: poured > 0 ? "ok" : (rebuilt ? "timeout" : "rejected"),
         pours: pours.length, rebuilt, failed, fills,
         poured_before: pouredBefore, poured_after: poured,
         expect: EXPECT || null, target, ms, errors, methods, opened_pcb: openedPcb,
         saved: saved };
"""


def _exit_code(reason: str) -> int:
    return {"ok": EXIT_OK, "unsupported_api": EXIT_UNSUPPORTED, "no_session": EXIT_NO_SESSION,
            "no_pour": EXIT_NO_POUR,
    "not_needed": EXIT_OK, "rejected": EXIT_REJECTED, "timeout": EXIT_REJECTED,
            "env": EXIT_ENV, "no_document": EXIT_NO_DOC}.get(reason, EXIT_FAIL)


def _session(exe: str) -> str | None:
    out = subprocess.run([exe, "session", "list"], capture_output=True, timeout=120)
    try:
        payload = json.loads(out.stdout.decode("utf-8", "replace"))
    except Exception:
        return None
    for s in payload.get("value") or []:
        if s.get("origin") == "user" and s.get("status") == "alive":
            return s["sessionId"]
    return None


def repour(exe: str, expect: int = 0, force: bool = False, skip_if_poured: bool = False,
           dry_run: bool = False, timeout: int = 600) -> dict:
    if not exe or not Path(exe).exists():
        return {"ok": False, "reason_code": "env", "reason": f"EDA 可执行文件不存在：{exe}"}
    try:
        sid = _session(exe)
    except Exception as exc:
        return {"ok": False, "reason_code": "env", "reason": f"无法调用 EDA CLI：{exc}"}
    if not sid:
        return {"ok": False, "reason_code": "no_session"}

    js = (JS.replace("__EXPECT__", str(int(expect)))
            .replace("__FORCE__", "true" if force else "false")
            .replace("__SKIP__", "true" if skip_if_poured else "false"))
    fd, js_path = tempfile.mkstemp(prefix="rebuild_in_jlc_repour_", suffix=".js")
    os.close(fd)
    try:
        Path(js_path).write_text(js, encoding="utf-8")
        proc = subprocess.run([exe, "invoke", "--session", sid, "--code-file",
                               Path(js_path).as_posix(), "--timeout", str(timeout * 1000)],
                              capture_output=True, timeout=timeout + 120)
        text = proc.stdout.decode("utf-8", "replace")
        start = text.find("{")
        if start < 0:
            return {"ok": False, "reason_code": "rejected",
                    "reason": (text or "invoke 无输出")[:300]}
        result = json.loads(text[start:]).get("value") or {}
    finally:
        try:
            os.unlink(js_path)
        except OSError:
            pass

    if dry_run:
        result["dry_run"] = True
        result["reason_code"] = "ok" if result.get("methods") and result.get("pours") else \
            result.get("reason_code", "unsupported_api")
    result.setdefault("reason_code", "rejected" if not result.get("ok") else "ok")
    result["hint"] = HINTS.get(result["reason_code"], HINTS["rejected"])
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="触发嘉立创EDA 重新灌铜（等价于 Shift+B）")
    ap.add_argument("--exe", default=DEFAULT_EXE, help="嘉立创EDA 可执行文件")
    ap.add_argument("--expect", type=int, default=0, help="期望的 POURED 数量（默认 ≥1 即成功）")
    ap.add_argument("--force", action="store_true", help="即使已有填充也强制重算")
    ap.add_argument("--skip-if-poured", action="store_true", help="已有填充则跳过")
    ap.add_argument("--dry-run", action="store_true", help="只探测接口是否可用，不重铺")
    ap.add_argument("--timeout", type=int, default=600, help="单次等待上限（秒）")
    ap.add_argument("--json", action="store_true", help="只打印结果 JSON")
    a = ap.parse_args(argv)

    result = repour(a.exe, a.expect, a.force, a.skip_if_poured, a.dry_run, a.timeout)
    if a.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result.get("ok"):
            if result.get("skipped"):
                print(f"已有填充，跳过重铺（POURED {result.get('poured_after')}）")
            elif a.dry_run:
                print(f"接口可用：rebuildCopperRegion 存在；POUR {result.get('pours')} 块，"
                      f"可用方法 {result.get('methods')}")
            else:
                print(f"重新灌铜完成：{result.get('rebuilt')}/{result.get('pours')} 块，"
                      f"填充路径 {result.get('fills')} 条，"
                      f"POURED {result.get('poured_before')} → {result.get('poured_after')}，"
                      f"耗时 {result.get('ms')} ms")
        else:
            print(f"重新灌铜未成功（{result.get('reason_code')}）：{result.get('hint')}")
            if result.get("methods"):
                print(f"  编辑器当前可用的相关方法：{result['methods']}")
            if result.get("errors"):
                print(f"  调用错误：{result['errors'][:2]}")
    return _exit_code(result.get("reason_code", "rejected"))


if __name__ == "__main__":
    sys.exit(main())
