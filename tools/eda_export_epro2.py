# -*- coding: utf-8 -*-
"""把磁盘上的 .eprj3 工程导出为 .epro2（用 EDA CLI 自己导）。"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile

DEFAULT_EXE = r"D:\Software\Electrical\lceda-proV4\lceda-pro.exe"

JS = """\
// 等工程就绪（path 打开的工程要几秒才加载完）
for (let i = 0; i < 120; i++) {
  const pcbs = await eda.dmt_Pcb.getAllPcbsInfo();
  if (pcbs && pcbs.length) break;
  await new Promise((r) => setTimeout(r, 500));
}
const name = "exported";
const file = await eda.sys_FileManager.getProjectFile(name, undefined, "epro2");
if (!file) throw new Error("getProjectFile 返回 undefined");
const buf = new Uint8Array(await file.arrayBuffer());
let bin = "";
const CH = 0x8000;
for (let i = 0; i < buf.length; i += CH) bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
return { ok: true, name: file.name || (name + ".epro2"), size: buf.length, b64: btoa(bin) };
"""


def run(exe: str, *args: str, timeout: int = 600) -> dict:
    proc = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
    text = (proc.stdout or b"").decode("utf-8", "replace").strip() or \
           (proc.stderr or b"").decode("utf-8", "replace").strip()
    start = text.find("{")
    if start < 0:
        raise RuntimeError(f"CLI 未返回 JSON：{text[:300]}")
    data = json.loads(text[start:])
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data.get("error"), ensure_ascii=False))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=".eprj3 → .epro2")
    ap.add_argument("project")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--exe", default=os.environ.get("JLC_EDA_EXE", DEFAULT_EXE))
    a = ap.parse_args(argv)

    project = os.path.abspath(a.project)
    opened = run(a.exe, "open", "--path", project.replace("\\", "/"), "--headless", "true")
    sid = opened["value"]["sessionId"]
    print("session:", sid)
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(JS)
            js = fh.name
        res = run(a.exe, "invoke", "--session", sid, "--code-file", js.replace("\\", "/"),
                  "--timeout", "300000")
        os.unlink(js)
        v = res["value"]
        data = base64.b64decode(v["b64"])
        out = a.out or os.path.join("out", os.path.splitext(os.path.basename(project))[0] + ".epro2")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        open(out, "wb").write(data)
        print(f"导出：{out}（{len(data)} 字节）")
        return out
    finally:
        run(a.exe, "session", "close", "--session", sid)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
