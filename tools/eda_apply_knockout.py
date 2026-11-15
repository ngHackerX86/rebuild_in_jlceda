# -*- coding: utf-8 -*-
"""把反显（knockout）文字的"反相"标志写进已经在 EDA 里打开的工程。

背景：KiCad 的 knockout 文字会写成 EDA STRING 的 `reverse=true`，但 **导入时会丢**
（属性面板显示"反相=否、反相扩展=0"），必须导入后再用 API 的 `modify` 补一次。

用法：
    python tools/eda_apply_knockout.py knockout_texts.json [--exe <lceda-pro.exe>]

sidecar 格式（转换器生成）：
    [{"text": "BOOT", "x": 1059.0, "y": -1035.0, "expansion": 4.9}, ...]
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

JS = r"""
const ITEMS = __ITEMS__;
const TOL = 30;          // mil，位置匹配容差
const arr = await eda.pcb_PrimitiveString.getAll() || [];
const touched = [], missed = [];
for (const want of ITEMS) {
  let best = null;
  for (const s of arr) {
    let txt = null, x = null, y = null;
    try { txt = String(s.getState_Text() || ""); } catch (e) { continue; }
    if (txt.trim() !== want.text.trim()) continue;
    try { x = s.getState_X(); y = s.getState_Y(); } catch (e) { continue; }
    if (Math.abs(x - want.x) > TOL || Math.abs(y - want.y) > TOL) continue;
    best = s;
    break;
  }
  if (!best) { missed.push(want.text); continue; }
  try {
    await eda.pcb_PrimitiveString.modify(best.getState_PrimitiveId(),
      { reverse: true, expansion: want.expansion });
    const after = await eda.pcb_PrimitiveString.get(best.getState_PrimitiveId());
    touched.push({ text: want.text, reverse: after.getState_Reverse(), expansion: after.getState_Expansion() });
  } catch (e) {
    missed.push(want.text + ":" + String(e).slice(0, 40));
  }
}
await eda.pcb_Document.save().catch(() => null);
return { ok: missed.length === 0, touched, missed, total: ITEMS.length };
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="补写反显（反相）文字标志")
    ap.add_argument("sidecar", help="转换器生成的 knockout_texts.json")
    ap.add_argument("--exe", default=DEFAULT_EXE)
    a = ap.parse_args(argv)

    items = json.loads(Path(a.sidecar).read_text(encoding="utf-8"))
    if not items:
        print("没有反显文字，跳过")
        return 0

    sessions = json.loads(subprocess.run([a.exe, "session", "list"], capture_output=True,
                                         timeout=120).stdout.decode("utf-8", "replace"))
    sid = next((s["sessionId"] for s in sessions.get("value") or []
                if s.get("origin") == "user" and s.get("status") == "alive"), None)
    if not sid:
        print("没有在线会话，跳过反相修补")
        return 1

    fd, js_path = tempfile.mkstemp(prefix="rebuild_in_jlc_reverse_", suffix=".js")
    os.close(fd)
    try:
        Path(js_path).write_text(JS.replace("__ITEMS__", json.dumps(items, ensure_ascii=False)),
                                 encoding="utf-8")
        proc = subprocess.run([a.exe, "invoke", "--session", sid, "--code-file",
                               Path(js_path).as_posix(), "--timeout", "300000"],
                              capture_output=True, timeout=420)
        text = proc.stdout.decode("utf-8", "replace")
        start = text.find("{")
        result = json.loads(text[start:]).get("value") or {} if start >= 0 else {}
    finally:
        try:
            os.unlink(js_path)
        except OSError:
            pass

    print(f"反相文字：需要 {result.get('total', len(items))} 条，成功 {len(result.get('touched') or [])} 条")
    if result.get("missed"):
        print("  未命中：", result["missed"][:6])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
