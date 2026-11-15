# -*- coding: utf-8 -*-
"""导入 .epro2 到当前账号工作区，并回读工程 UUID/名称。

用法：python tools/eda_import_verify.py <工程.epro2> [--exe <lceda-pro.exe>] [--proj-name 名称]

注意：账号里若已存在**同名**工程，编辑器不会新建（返回 imported=null）。
流水线每次会带时间戳生成唯一名称。

临时 JS 写在系统临时目录，不依赖任何作者本机路径。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_EXE = os.environ.get("JLC_EDA_EXE", r"D:\Software\Electrical\lceda-proV4\lceda-pro.exe")

JS = r"""
const b64 = "__B64__";
const bin = atob(b64);
const bytes = new Uint8Array(bin.length);
for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
const file = new File([bytes], "__NAME__");

function txt(e) { return (e && (e.message || String(e))) || String(e); }
function plain(v, d) {
  d = d || 0;
  if (v === null || typeof v !== "object") return v;
  if (d > 2) return "[deep]";
  const o = {};
  for (const k of Object.getOwnPropertyNames(v)) {
    try { o[k] = plain(v[k], d + 1); } catch (_) { o[k] = "<getter>"; }
  }
  return o;
}
const log = [];
const before = await eda.dmt_Project.getAllProjectsUuid();

const user = await eda.sys_Environment.getUserInfo();
let imported = null, err = null;
try {
  imported = await eda.sys_FileManager.importProjectByProjectFile(file, "EasyEDA Pro", undefined, {
    operation: "New Project",
    newProjectOwnerTeamUuid: user.uuid,
    newProjectName: "__PROJNAME__",
    newProjectFriendlyName: "__PROJNAME__",
    newProjectDescription: "KiCad 重建（文档级注入封装）→ 账号工作区",
  });
} catch (e) { err = txt(e); }
await new Promise((r) => setTimeout(r, 3000));

const after = await eda.dmt_Project.getAllProjectsUuid();
const cur = await eda.dmt_Project.getCurrentProjectInfo();

return {
  ok: !err,
  err,
  imported: plain(imported),
  beforeCount: Array.isArray(before) ? before.length : before,
  afterCount: Array.isArray(after) ? after.length : after,
  added: Array.isArray(after) && Array.isArray(before)
    ? after.filter((u) => before.indexOf(u) < 0) : null,
  current: cur ? plain(cur) : null,
  log,
};
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    exe = DEFAULT_EXE
    proj_name = None
    if "--exe" in argv:
        i = argv.index("--exe")
        exe = argv[i + 1]
        del argv[i:i + 2]
    if "--proj-name" in argv:
        i = argv.index("--proj-name")
        proj_name = argv[i + 1]
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 2
    src = Path(argv[0]).expanduser().resolve()
    if not src.exists():
        print(f"工程文件不存在：{src}")
        return 2

    data = src.read_bytes()
    name = src.stem
    js = (JS.replace("__B64__", base64.b64encode(data).decode())
            .replace("__NAME__", src.name)
            .replace("__PROJNAME__", proj_name or ("KiCad_" + name)))
    fd, js_path = tempfile.mkstemp(prefix="rebuild_in_jlc_import_", suffix=".js")
    os.close(fd)
    try:
        Path(js_path).write_text(js, encoding="utf-8")
        sessions = subprocess.run([exe, "session", "list"], capture_output=True, timeout=120)
        payload = json.loads(sessions.stdout.decode("utf-8", "replace"))
        sid = next((s["sessionId"] for s in payload["value"]
                    if s.get("origin") == "user" and s.get("status") == "alive"), None)
        if not sid:
            print("没有在线的用户会话：请先在嘉立创EDA 登录并打开任意工程")
            return 1
        proc = subprocess.run([exe, "invoke", "--session", sid, "--code-file",
                               Path(js_path).as_posix(), "--timeout", "300000"],
                              capture_output=True, timeout=600)
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        print(out[:2000])
        start = out.find("{")
        if start < 0:
            return 1
        result = json.loads(out[start:])
        value = result.get("value") or {}
        if value.get("ok") and value.get("imported"):
            return 0
        current = value.get("current") or {}
        if current.get("friendlyName"):
            print(f"导入没有创建新工程：账号里已存在同名工程「{current.get('friendlyName')}」，"
                  f"请换一个 --proj-name。", file=sys.stderr)
        else:
            print("导入未返回工程信息，请检查 EDA 是否可写账号工作区。", file=sys.stderr)
        return 1
    finally:
        try:
            os.unlink(js_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
