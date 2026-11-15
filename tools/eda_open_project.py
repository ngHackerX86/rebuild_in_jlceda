# -*- coding: utf-8 -*-
"""打开指定工程（带重试）并打开其 PCB 文档，确保后续 DRC 作用在正确工程上。

用法：python tools/eda_open_project.py <项目 uuid 或 名称> [--exe <lceda-pro.exe>] [--expect-uuid <uuid>]

带 --expect-uuid 时会校验当前打开的工程 UUID，不匹配即视为失败（避免在错误工程上继续）。

临时 JS 写在系统临时目录，不依赖任何作者本机路径。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_EXE = os.environ.get("JLC_EDA_EXE", r"D:\Software\Electrical\lceda-proV4\lceda-pro.exe")

JS_TEMPLATE = r"""
const PROJ = "__PROJ__";
const EXPECT_UUID = "__EXPECT__";
const log = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 先看当前打开的是不是目标工程；不是就先把已打开的文档关掉，
// 否则 openProject 可能一直卡在当前工程（实测有一次卡了 88 秒仍未切换）。
try {
  const cur0 = await eda.dmt_Project.getCurrentProjectInfo().catch(() => null);
  if (EXPECT_UUID && cur0 && cur0.uuid && cur0.uuid !== EXPECT_UUID) {
    const pcbs0 = (await eda.dmt_Pcb.getAllPcbsInfo().catch(() => null)) || [];
    for (const p of pcbs0) {
      try { await eda.dmt_EditorControl.closeDocument(p.uuid); } catch (e) {}
    }
    try { await eda.pcb_Document.save(); } catch (e) {}   // 先落盘，避免阻塞切换
    log.push("closed:" + pcbs0.length);
    await sleep(1500);
  }
} catch (e) { log.push("precheck:" + String(e).slice(0, 30)); }

let opened = false;
for (let i = 0; i < 10 && !opened; i++) {
  try { opened = await eda.dmt_Project.openProject(PROJ); } catch (e) { log.push("open:" + String(e).slice(0, 40)); }
  if (!opened) await new Promise((r) => setTimeout(r, 2500));
}
try { await eda.dmt_Project.waitForReady(PROJ, 60000); } catch (e) {}
const info = await eda.dmt_Project.getCurrentProjectInfo().catch(() => null);
const curUuid = info ? info.uuid : null;
const curName = info ? (info.name || info.friendlyName) : null;
const expectUuid = "__EXPECT__";
const uuidMatch = expectUuid ? (curUuid === expectUuid) : null;
let pcbs = null;
for (let i = 0; i < 60; i++) {
  pcbs = await eda.dmt_Pcb.getAllPcbsInfo().catch(() => null);
  if (pcbs && pcbs.length) break;
  await new Promise((r) => setTimeout(r, 500));
}
let pcbUuid = null;
if (pcbs && pcbs.length) {
  pcbUuid = pcbs[0].uuid;
  await eda.dmt_EditorControl.openDocument(pcbUuid);
  await new Promise((r) => setTimeout(r, 3000));
}
const pads = await eda.pcb_PrimitivePad.getAll().catch(() => null);
const comps = await eda.pcb_PrimitiveComponent.getAll().catch(() => null);
const lines = await eda.pcb_PrimitiveLine.getAll().catch(() => null);
return { ok: !!pcbUuid && opened && (expectUuid ? uuidMatch === true : true), opened,
         project: curName, expectUuid: expectUuid || null, uuidMatch,
         currentUuid: curUuid, pcbUuid,
         pads: Array.isArray(pads) ? pads.length : -1,
         comps: Array.isArray(comps) ? comps.length : -1,
         lines: Array.isArray(lines) ? lines.length : -1,
         log: log.slice(0, 4) };
"""


def open_project(exe: str, project: str, expect_uuid: str = "",
                 timeout: int = 900) -> dict | None:
    """打开工程与 PCB；返回 EDA 的 JSON 结果（失败返回 None）。"""
    fd, js_path = tempfile.mkstemp(prefix="rebuild_in_jlc_open_", suffix=".js")
    os.close(fd)
    try:
        Path(js_path).write_text(JS_TEMPLATE.replace("__PROJ__", project)
                                 .replace("__EXPECT__", expect_uuid), encoding="utf-8")
        sessions = json.loads(subprocess.run([exe, "session", "list"], capture_output=True,
                                             timeout=120).stdout.decode("utf-8", "replace"))
        sid = next((s["sessionId"] for s in sessions["value"]
                    if s.get("origin") == "user" and s.get("status") == "alive"), None)
        if not sid:
            print("没有在线的用户会话：请先在嘉立创EDA 登录并打开任意工程")
            return None
        out = subprocess.run([exe, "invoke", "--session", sid, "--code-file",
                              Path(js_path).as_posix(), "--timeout", "600000"],
                             capture_output=True, timeout=timeout)
        text = out.stdout.decode("utf-8", "replace")
        start = text.find("{")
        return json.loads(text[start:]) if start >= 0 else None
    finally:
        try:
            os.unlink(js_path)
        except OSError:
            pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    exe = DEFAULT_EXE
    expect = ""
    if "--exe" in argv:
        i = argv.index("--exe")
        exe = argv[i + 1]
        del argv[i:i + 2]
    if "--expect-uuid" in argv:
        i = argv.index("--expect-uuid")
        expect = argv[i + 1]
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 2
    result = open_project(exe, argv[0], expect)
    if not result:
        print("打开失败")
        return 1
    print(json.dumps(result, ensure_ascii=False)[:900])
    ok = bool((result.get("value") or {}).get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
