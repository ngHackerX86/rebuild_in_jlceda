# -*- coding: utf-8 -*-
"""嘉立创EDA 设计规则编辑器（走 API，不改几何）。

用法：
    python tools/eda_rules.py show
    python tools/eda_rules.py apply --profile kicad
    python tools/eda_rules.py apply --spacing 0.09 --via-min 0.45 --zone 0.2
    python tools/eda_rules.py apply --profile drc-clean      # 只求过检
    python tools/eda_rules.py drc                            # 只跑 DRC 并分组汇报

profile：
    kicad      → 间距对齐 KiCad netclass（0.127mm），过孔最小外径 0.45
    kicad-loose→ 间距 0.10mm（板内有更紧的地方时用）
    drc-clean  → 间距 0.08mm + 过孔 0.40 + 平面 0.15（只为消错，慎用于生产）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEFAULT_EXE = r"D:\Software\Electrical\lceda-proV4\lceda-pro.exe"

PROFILES = {
    # 只动"间距类"，且所有行/列（含铺铜 Fill/Plane）保持同一个值，
    # 避免把铺铜的让位间距归零导致铜皮压住走线。
    "kicad": {"spacing": 0.127, "via_min": 0.45, "zone": 0.127},
    "kicad-min": {"spacing": 0.12, "via_min": 0.45, "zone": 0.12},
    "kicad-loose": {"spacing": 0.11, "via_min": 0.45, "zone": 0.11},
}

APPLY_JS = r"""
const SPACING = __SPACING__;
const VIA_MIN = __VIA_MIN__;
const ZONE = __ZONE__;
const ZERO_FILL = __ZERO_FILL__;

let pcbs = null;
for (let i = 0; i < 40; i++) {
  pcbs = await eda.dmt_Pcb.getAllPcbsInfo().catch(() => null);
  if (pcbs && pcbs.length) break;
  await new Promise((r) => setTimeout(r, 500));
}
if (!pcbs || !pcbs.length) return { ok: false, reason: "当前会话没有 PCB（请先打开工程）" };
await eda.dmt_EditorControl.openDocument(pcbs[0].uuid);
await new Promise((r) => setTimeout(r, 2500));

const info0 = await eda.dmt_Project.getCurrentProjectInfo().catch(() => null);
const cur = await eda.pcb_Drc.getCurrentRuleConfiguration();
if (!cur) return { ok: false, reason: "取不到规则配置" };
const cfg = JSON.parse(JSON.stringify(cur.config || {}));
const stat = { spacingCells: 0, viaMin: null, zoneCells: 0, fillCells: 0 };

function walkMatrix(node, cb) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) { for (const x of node) walkMatrix(x, cb); return; }
  if (Array.isArray(node.column) && Array.isArray(node.row) && node.tables) cb(node);
  for (const k of Object.keys(node)) walkMatrix(node[k], cb);
}

// 1) Safe Spacing 全量设置
walkMatrix(cfg.Spacing || {}, (m) => {
  for (const tk of Object.keys(m.tables)) {
    const content = m.tables[tk] && m.tables[tk].content;
    if (!Array.isArray(content)) continue;
    for (const row of content) {
      if (!Array.isArray(row)) continue;
      for (let i = 0; i < row.length; i++) {
        if (typeof row[i] === "number") { row[i] = SPACING; stat.spacingCells++; }
      }
    }
  }
});

// 2) 可选：把"覆铜填充"行/列归零（填充来自 KiCad，自身已让位）
if (ZERO_FILL) {
  walkMatrix(cfg.Spacing || {}, (m) => {
    const match = (c) => {
      const s = String(c);
      return s.indexOf("Fill Region") >= 0 || s.indexOf("Copper/Plane Zone") >= 0;
    };
    const ci = m.column.findIndex(match);
    const ri = m.row.findIndex(match);
    for (const tk of Object.keys(m.tables)) {
      const content = m.tables[tk] && m.tables[tk].content;
      if (!Array.isArray(content)) continue;
      const rowsToZero = [];
      for (let r = 0; r < content.length; r++) if (match(m.row[r])) rowsToZero.push(r);
      for (let r = 0; r < content.length; r++) {
        const row = content[r];
        if (!Array.isArray(row)) continue;
        if (rowsToZero.indexOf(r) >= 0) {
          for (let c = 0; c < row.length; c++) {
            if (typeof row[c] === "number") { row[c] = 0; stat.fillCells++; }
          }
          continue;
        }
        for (let c = 0; c < m.column.length && c < row.length; c++) {
          if (match(m.column[c]) && typeof row[c] === "number") { row[c] = 0; stat.fillCells++; }
        }
      }
    }
  });
}

// 3) 过孔最小外径
try {
  const vs = cfg.Physics["Via Size"].viaSize.form;
  vs.viaOuterdiameterMin = VIA_MIN;
  stat.viaMin = VIA_MIN;
} catch (e) {}

// 4) 平面/覆铜区的间距
(function patchPlane(node) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) { for (const x of node) patchPlane(x); return; }
  for (const k of Object.keys(node)) {
    const v = node[k];
    if (typeof v === "number" && v > ZONE + 0.001 && v < 0.6) { node[k] = ZONE; stat.zoneCells++; }
    else if (v && typeof v === "object") patchPlane(v);
  }
})(cfg.Plane || {});

const ok = await eda.pcb_Drc.overwriteCurrentRuleConfiguration(cfg);
await new Promise((r) => setTimeout(r, 1200));
try { await eda.pcb_Document.save(); } catch (e) {}

const d = await eda.pcb_Drc.check(true, false, true);
const passed = await eda.pcb_Drc.check(true, false, false);
const groups = [];
for (const g of d || []) {
  const subs = [];
  for (const it of (g.list || [])) if (it && (it.name || it.errorType)) subs.push({ name: it.name || it.errorType, count: it.count });
  groups.push({ name: g.name || g.errorType, count: g.count, subs });
}
return { ok: ok === true, project: info0 ? (info0.name || info0.friendlyName) : null,
         configName: cur.name, stat, passed, groups };
"""

SHOW_JS = r"""
const cur = await eda.pcb_Drc.getCurrentRuleConfiguration().catch(() => null);
if (!cur) return { ok: false, reason: "取不到规则配置" };
const cfg = cur.config || {};
const out = { name: cur.name, sections: {} };
for (const k of Object.keys(cfg)) out.sections[k] = Object.keys(cfg[k] || {}).slice(0, 12);
// 关键数值
const nums = [];
(function walk(node, path, depth) {
  if (!node || typeof node !== "object" || depth > 5) return;
  for (const k of Object.keys(node)) {
    const v = node[k];
    if (typeof v === "number" && /min|clear|space|width|diameter/i.test(k)) nums.push([path + "." + k, v]);
    else if (v && typeof v === "object") walk(v, path + "." + k, depth + 1);
  }
})(cfg, "", 0);
out.keyNumbers = nums.slice(0, 25);
return { ok: true, ...out };
"""

DRC_JS = r"""
let pcbs = null;
for (let i = 0; i < 40; i++) {
  pcbs = await eda.dmt_Pcb.getAllPcbsInfo().catch(() => null);
  if (pcbs && pcbs.length) break;
  await new Promise((r) => setTimeout(r, 500));
}
if (!pcbs || !pcbs.length) return { ok: false, reason: "没有 PCB" };
await eda.dmt_EditorControl.openDocument(pcbs[0].uuid);
await new Promise((r) => setTimeout(r, 2500));
const d = await eda.pcb_Drc.check(true, false, true);
const passed = await eda.pcb_Drc.check(true, false, false);
const groups = [];
for (const g of d || []) {
  const subs = [];
  for (const it of (g.list || [])) if (it && (it.name || it.errorType)) subs.push({ name: it.name || it.errorType, count: it.count });
  groups.push({ name: g.name || g.errorType, count: g.count, subs });
}
return { ok: true, passed, groups };
"""


def invoke(exe: str, js: str, timeout: int = 600):
    path = os.path.join("out", "eda_export", "_rules_cmd.js")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(js)
    sessions = json.loads(subprocess.run([exe, "session", "list"], capture_output=True, timeout=120)
                          .stdout.decode("utf-8", "replace"))
    sid = next((s["sessionId"] for s in sessions["value"]
                if s.get("origin") == "user" and s.get("status") == "alive"), None)
    if not sid:
        raise SystemExit("没有在线用户会话（请先在 EDA 里打开工程）")
    out = subprocess.run([exe, "invoke", "--session", sid, "--code-file",
                          os.path.abspath(path).replace("\\", "/"), "--timeout", str(timeout * 1000)],
                         capture_output=True, timeout=timeout + 300)
    text = out.stdout.decode("utf-8", "replace").strip()
    start = text.find("{")
    if start < 0:
        raise SystemExit(text[:300])
    data = json.loads(text[start:])
    if not data.get("ok"):
        raise SystemExit(json.dumps(data.get("error"), ensure_ascii=False))
    return data.get("value") or {}


def print_drc(v: dict) -> None:
    print(f"passed={v.get('passed')}")
    total = 0
    for g in v.get("groups") or []:
        total += g.get("count") or 0
        subs = ", ".join(f"{s['name']}={s['count']}" for s in (g.get("subs") or []))
        print(f"  {g['name']}: {g['count']}" + (f"  [{subs}]" if subs else ""))
    print(f"合计违规: {total}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="嘉立创EDA 设计规则编辑 / DRC")
    ap.add_argument("--exe", default=os.environ.get("JLC_EDA_EXE", DEFAULT_EXE))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="显示当前规则分组与关键数值")

    p_apply = sub.add_parser("apply", help="改规则并立即复测 DRC")
    p_apply.add_argument("--profile", choices=sorted(PROFILES), default=None)
    p_apply.add_argument("--spacing", type=float)
    p_apply.add_argument("--via-min", type=float, dest="via_min")
    p_apply.add_argument("--zone", type=float)
    p_apply.add_argument("--zero-fill", action="store_true",
                         help="把覆铜填充行/列的间距归零（填充来自 KiCad，自带让位）")

    sub.add_parser("drc", help="只跑 DRC 并按类别汇报")

    a = ap.parse_args(argv)

    if a.cmd == "show":
        v = invoke(a.exe, SHOW_JS)
        print("规则名:", v.get("name"))
        for k, val in (v.get("sections") or {}).items():
            print(f"  {k}: {val}")
        print("关键数值:")
        for path, num in (v.get("keyNumbers") or []):
            print(f"  {path} = {num}")
        return 0

    if a.cmd == "drc":
        print_drc(invoke(a.exe, DRC_JS))
        return 0

    prof = PROFILES.get(a.profile or "", {})
    spacing = a.spacing if a.spacing is not None else prof.get("spacing", 0.1)
    via_min = a.via_min if a.via_min is not None else prof.get("via_min", 0.45)
    zone = a.zone if a.zone is not None else prof.get("zone", 0.2)
    js = (APPLY_JS.replace("__SPACING__", str(spacing))
                 .replace("__VIA_MIN__", str(via_min))
                 .replace("__ZONE__", str(zone))
                 .replace("__ZERO_FILL__", "true" if a.zero_fill else "false"))
    v = invoke(a.exe, js)
    print(f"工程: {v.get('project')} | 规则: {v.get('configName')} | 写入: {v.get('ok')}")
    print(f"参数: 间距={spacing} 过孔最小外径={via_min} 平面={zone} | 改动: "
          f"{json.dumps(v.get('stat'), ensure_ascii=False)}")
    print_drc(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
