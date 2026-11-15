"""把一组成品 Gerber/钻孔文件重新命名、打包成嘉立创下单可直接上传的 zip。

设计约束（对应评审 S-01 / S-07 / S-03）：

* **孔数据守恒**：默认不允许因为"孔位重合"就丢弃文件。只有当能证明整份 B 是 A 的
  子集（单位、坐标基准、镀层、孔型、孔径、完整几何全部一致）时，才允许省略 B，
  并把 A/B 的哈希、孔数、判定依据写进清单。
* **部分重合 / 同坐标异孔径**：一律报告冲突并**阻断**，不自动取舍。
* **目标名唯一**：同层两份文件、或两个不同钻孔映射到同一文件名，直接报错，
  不允许"后写覆盖"。
* **先写临时包，校验后再原子替换**，避免失败时留下半成品；失败包另起名字。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from dataclasses import dataclass, field

from . import excellon, gerber, layers
from .native import JLC_README_BYTES, JLC_README_NAME


class PackagingError(RuntimeError):
    """打包冲突：必须人工处理，不能自动取舍。"""


@dataclass
class PlanItem:
    src: str
    dst: str
    note: str


@dataclass
class DrillPlan:
    kept: list[PlanItem] = field(default_factory=list)
    omitted: list[tuple[str, str, str]] = field(default_factory=list)  # (被省略, 依据, 说明)
    conflicts: list[str] = field(default_factory=list)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _drill_signature(d: excellon.DrillFile):
    """把钻孔文件的"实际几何"规范化，用于集合比较（含孔径、镀层、孔型）。"""
    holes = {(round(x, 3), round(y, 3), round(d.tools[t].diameter, 4) if t in d.tools else 0.0)
             for (x, y, t) in d.holes}
    slots = {(round(a, 3), round(b, 3), round(c, 3), round(e, 3),
              round(d.tools[t].diameter, 4) if t in d.tools else 0.0)
             for (a, b, c, e, t) in d.slots}
    return holes, slots


def _subset_evidence(small: excellon.DrillFile, big: excellon.DrillFile,
                     small_name: str, big_name: str) -> str | None:
    """若 small 能被 big 完整覆盖，返回判定依据；否则返回 None。"""
    if small.units != big.units:
        return None
    if small.plated != big.plated:
        return None                       # PTH/NPTH 不能互相覆盖
    if small.unsupported or big.unsupported:
        return None                       # 解析不完整时不做任何省略
    sh, ss = _drill_signature(small)
    bh, bs = _drill_signature(big)
    if not (sh <= bh and ss <= bs):
        return None
    if not (sh or ss):
        return None
    return (f"{small_name} 的 {len(sh)} 孔 + {len(ss)} 槽全部包含于 {big_name}"
            f"（{len(bh)} 孔 + {len(bs)} 槽，孔径/镀层/孔型一致）")


def _tool_string() -> str:
    try:
        import pcbnew  # noqa: F401
        return "KiCad pcbnew " + getattr(pcbnew, "Version", lambda: "")()
    except Exception:
        return "kicad_jlc_exporter"


def _is_drill_content(path: str) -> bool:
    d = excellon.parse(path)
    return bool(d.tools) and bool(d.holes or d.slots)


def plan(files: list[str], board: str) -> tuple[list[PlanItem], DrillPlan, list[str]]:
    """生成重命名计划；返回 (计划, 钻孔计划, 警告)。冲突抛 PackagingError。"""
    items: list[PlanItem] = []
    warns: list[str] = []
    dplan = DrillPlan()
    drills_src: list[str] = []

    for f in sorted(files):
        ki, why = layers.classify(f)
        if ki is not None:
            if ki.kind in ("paste", "silk"):
                g = gerber.parse(f)
                if not (g.flashes or g.strokes or g.regions):
                    warns.append(f"{os.path.basename(f)}：{ki.kicad} 是空层（无任何图形），不打包")
                    continue
            if ki.kind == "other":
                warns.append(f"{os.path.basename(f)}：{ki.kicad} 非制造层，放入 extras/")
                items.append(PlanItem(f, f"extras/{os.path.basename(f)}", f"{ki.kicad}（非制造层）"))
                continue
            items.append(PlanItem(f, f"{board}{ki.suffix}", f"{ki.kicad} ← {why}"))
            continue
        ext = os.path.splitext(f)[1].upper()
        if ext == ".GBRJOB":
            warns.append(f"{os.path.basename(f)}：KiCad Gerber Job 文件，嘉立创不解析，不打包")
            continue
        if ext in (".DRL", ".TXT", ".NCD") and _is_drill_content(f):
            drills_src.append(f)
            continue
        warns.append(f"{os.path.basename(f)}：无法识别，放入 extras/")
        items.append(PlanItem(f, f"extras/{os.path.basename(f)}", why))

    # ---- 钻孔：先判内容，再做严格子集去重（不按比例丢文件）----
    drills = [(s, excellon.parse(s)) for s in drills_src]
    pth = [(s, d) for (s, d) in drills if d.plated is not False]
    npth = [(s, d) for (s, d) in drills if d.plated is False]

    kept_pth: list[tuple[str, excellon.DrillFile]] = []
    for s, d in sorted(pth, key=lambda t: -(t[1].total_features)):
        for s2, d2 in kept_pth:
            ev = _subset_evidence(d, d2, os.path.basename(s), os.path.basename(s2))
            if ev:
                dplan.omitted.append((os.path.basename(s), "整份子集", ev))
                break
        else:
            kept_pth.append((s, d))

    # 剩余文件之间是否"部分重合"？那就阻断，不猜
    for i in range(len(kept_pth)):
        for j in range(i + 1, len(kept_pth)):
            (sa, da), (sb, db) = kept_pth[i], kept_pth[j]
            # 只比坐标：同坐标但孔径不同同样是冲突（同一位置钻两个孔径=废板）
            ha = {(x, y) for (x, y, _d) in _drill_signature(da)[0]}
            hb = {(x, y) for (x, y, _d) in _drill_signature(db)[0]}
            inter = ha & hb
            if inter:
                dplan.conflicts.append(
                    f"{os.path.basename(sa)} 与 {os.path.basename(sb)} 有 {len(inter)} 个"
                    f"坐标重合的孔，但谁也不是谁的子集：可能是 PTH/NPTH 拆分、"
                    f"也可能是同坐标异孔径，必须人工确认后再打包")

    if len(pth) > 1 and len(kept_pth) == len(pth) and not dplan.conflicts:
        warns.append("多个 PTH 钻孔文件互不包含：已全部保留，请确认嘉立创取哪一份")

    # 命名：一份 PTH 用 <板>-PTH.DRL，多份则加序号
    used = set()
    for idx, (s, d) in enumerate(kept_pth, 1):
        base = layers.DRILL_SUFFIX_PTH if len(kept_pth) == 1 else f"-PTH{idx}.DRL"
        dst = f"{board}{base}"
        used.add(dst)
        dplan.kept.append(PlanItem(s, dst, f"钻孔 PTH，{d.total_holes} 孔 + {d.total_slots} 槽"))
    for idx, (s, d) in enumerate(npth, 1):
        base = layers.DRILL_SUFFIX_NPTH if len(npth) == 1 else f"-NPTH{idx}.DRL"
        dst = f"{board}{base}"
        if dst in used:
            raise PackagingError(f"钻孔目标名冲突：{dst}")
        used.add(dst)
        dplan.kept.append(PlanItem(s, dst, f"钻孔 NPTH，{d.total_holes} 孔 + {d.total_slots} 槽"))

    items += dplan.kept

    # ---- 目标名唯一性（S-07）----
    names = [it.dst for it in items]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise PackagingError("打包目标名重复：" + "、".join(sorted(dup))
                             + "（同层两份文件或命名规则冲突，必须显式选择）")

    if dplan.conflicts:
        raise PackagingError("钻孔冲突：\n  - " + "\n  - ".join(dplan.conflicts))

    return items, dplan, warns


def build(files: list[str], out_zip: str, board: str | None = None,
          report=None, gate=None, notes: bool = True, manifest: bool = True) -> dict:
    """打包。`gate` 不为空且未允许发布时，只写诊断包。"""
    board = board or os.path.basename(os.path.dirname(out_zip)) or "PCB"
    items, dplan, warns = plan(files, board)

    release = True if gate is None else bool(getattr(gate, "publishable", gate.release))
    if not release:
        base, ext = os.path.splitext(out_zip)
        out_zip = f"{base}_DIAGNOSTIC{ext}"
        warns.append("发布判定未通过：本次只生成诊断包（文件名带 _DIAGNOSTIC），"
                     "不得直接上传投板")

    os.makedirs(os.path.dirname(os.path.abspath(out_zip)), exist_ok=True)
    tmp = out_zip + ".part"

    filelist_lines = []
    hashes = {}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for it in items:
            z.write(it.src, it.dst)
            hashes[it.dst] = _sha256(it.src)
            filelist_lines.append(f"  {it.dst:<28} {it.note}")

        drillnote = "\n".join(f"  {it.dst}: {it.note}" for it in dplan.kept) or "  （无钻孔文件）"
        if dplan.omitted:
            drillnote += "\n  已省略（整份子集，证据见清单）：" + \
                         "；".join(f"{a} ⊂ {c}" for a, _b, c in dplan.omitted)

        if notes:
            # 与嘉立创EDA 原文一致（评审/用户要求：包内说明文件不做自造内容）
            z.writestr(JLC_README_NAME, JLC_README_BYTES)

        if manifest:
            man = {
                "board": board,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "release": release,
                "tool": _tool_string(),
                "files": hashes,
                "drills": {
                    "kept": [{"file": it.dst, "note": it.note} for it in dplan.kept],
                    "omitted": [{"file": a, "reason": b, "evidence": c}
                                for a, b, c in dplan.omitted],
                },
                "gate": gate.to_manifest() if gate is not None else None,
                "preflight": report.to_manifest() if report is not None else None,
                "warnings": warns,
            }
            z.writestr("jlc_manifest.json",
                       json.dumps(man, ensure_ascii=False, indent=1))

    # 校验：能正常打开、条目数与计划一致，再原子替换
    with zipfile.ZipFile(tmp) as z:
        got = set(z.namelist())
    expected = {it.dst for it in items} | ({"PCB下单必读.txt"} if notes else set()) | \
               ({"jlc_manifest.json"} if manifest else set())
    missing = expected - got
    if missing:
        os.remove(tmp)
        raise PackagingError(f"打包校验失败，缺少条目：{sorted(missing)}")

    os.replace(tmp, out_zip)
    return {"zip": out_zip, "items": items, "warnings": warns,
            "omitted": dplan.omitted, "release": release,
            "size": os.path.getsize(out_zip)}
