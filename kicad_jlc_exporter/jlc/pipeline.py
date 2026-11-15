"""端到端流水线：KiCad 板 → Gerber → 预检 → 发布判定 → 嘉立创 zip。

评审 S-02 / S-03 / S-08 / S-09 / S-10 之后的关键约束：

* **层清单来自实际铜层**，不再有"默认双面"；拿不到板子对象时必须显式传层；
* **规则只加载一次**，文件级、板级、打包全部用同一份；
* **DRC 是独立阶段**（`kicad-cli pcb drc --refill-zones`），未执行即视为覆盖不全；
* **每次导出到新的 staging 目录**，只收集本次产物，避免旧文件混入；
* **发布判定 gate 决定产物名字**：不可发布时只写 `*_DIAGNOSTIC.zip`。
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from . import board_rules, flying_probe, gate as gate_mod, native, package, preflight
from .export_kicad import (ExportOptions, export, plan_layers, run_drc,
                          targets_from_names)


@dataclass
class RunResult:
    outdir: str
    staging: str
    zip_path: str | None = None
    report: object | None = None
    gate: gate_mod.GateDecision | None = None
    board_issues: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    drc_text: str = ""

    def to_text(self) -> str:
        parts = []
        if self.report is not None:
            parts.append(self.report.to_text())
        if self.board_issues:
            parts.append("--- 板级检查 ---")
            for i in self.board_issues:
                parts.append(f"  [{i.level}] [{i.code}] {i.message}")
        if self.drc_text:
            parts.append("--- DRC ---")
            parts.append(self.drc_text.rstrip())
        if self.gate is not None:
            parts.append(self.gate.to_text())
        if self.warnings:
            parts.append("--- 打包阶段 ---")
            parts += [f"  {w}" for w in self.warnings]
        if self.zip_path:
            parts.append(f"\n成品：{self.zip_path}")
        return "\n".join(parts)


def resolve_targets(board=None, layers: list[str] | None = None,
                    with_doc_layer: bool = False):
    """决定导出哪些层（返回 LayerTarget 列表）。

    显式清单优先（便于复现/CI），但随后会拿源板的铜层数对照校验——
    用户写错清单时必须被拦住，而不是用"自己的清单"自证清白（评审 S-02）。
    """
    if layers:
        return targets_from_names(list(layers))
    if board is not None:
        return plan_layers(board, with_doc_layer=with_doc_layer)
    raise ValueError(
        "无法确定层清单：没有板子对象时必须显式传 layers（避免多层板漏内层）。\n"
        "  例如：layers=['F.Cu','In1.Cu','In2.Cu','B.Cu','F.SilkS','B.SilkS',\n"
        "                'F.Mask','B.Mask','F.Paste','B.Paste','Edge.Cuts']")


def run(board_path: str, outdir: str | None = None, *,
        board_name: str | None = None,
        layers: list[str] | None = None,
        merge_pth_npth: bool = False,
        subtract_mask_from_silk: bool = True,
        precision: int = 5,
        make_zip: bool = True,
        with_flying_probe: bool | None = None,   # None = 按命名风格决定（jlc 默认开）
        rules: dict | None = None,
        rules_path: str | None = None,
        prefer_cli: bool = False,
        allow_skip_drc: bool = False,
        drc_waivers: dict[str, int] | None = None,
        naming: str = "jlc",
        x2_hints: bool = False,
        easyeda_version: str = native.EASYEDA_VERSION,
        easyeda_ts: str | None = None,
        with_doc_layer: bool | None = None,     # None = 按命名风格决定（jlc 默认开）
        board=None) -> RunResult:
    from . import export_kicad

    stem = os.path.splitext(os.path.basename(board_path))[0] if board_path else "PCB"
    name = board_name or stem
    # 绝对路径：pcbnew 的 SetOutputDirectory 会相对"板文件所在目录"解释相对路径
    outdir = os.path.abspath(outdir or os.path.join(
        os.path.dirname(os.path.abspath(board_path)), "jlc_fab"))

    # 每次导出到独立 staging，杜绝旧文件混入（S-10）
    staging = os.path.abspath(os.path.join(outdir, "staging_" + time.strftime("%Y%m%d_%H%M%S")))
    os.makedirs(staging, exist_ok=True)

    # 规则只加载一次，全流程共用（S-09）
    rule_set = rules if rules is not None else preflight.load_rules(rules_path)
    result = RunResult(outdir=outdir, staging=staging)

    # ---- 板子对象（能拿到就拿到，拿不到也把话说清楚）----
    if board is None:
        try:
            import pcbnew
            board = pcbnew.LoadBoard(board_path)
        except Exception as exc:                     # noqa: BLE001
            result.warnings.append(f"没有板子对象（{exc}）：将跳过板级检查与内层推断")

    if with_flying_probe is None:
        with_flying_probe = (naming == "jlc")     # 嘉立创EDA 的包里 always 带这个文件
    if with_doc_layer is None:
        with_doc_layer = (naming == "jlc")        # 同上：GDL 文档层
    targets = resolve_targets(board, layers, with_doc_layer=with_doc_layer)
    use_layers = [t.plot_name for t in targets]        # 给绘图器/CLI 用的真实层名
    copper_layers = [t for t in targets if t.copper]

    # 源板真实铜层数（不是"请求了几层"）：S-02 的关键——
    # 用户给错清单时，必须拿源板层栈对照，而不是拿自己的清单自证清白。
    src_copper = None
    if board is not None:
        try:
            src_copper = int(board.GetCopperLayerCount())
        except Exception:
            src_copper = None

    opt = ExportOptions(outdir=staging, board_name=name, precision=precision,
                        merge_pth_npth=merge_pth_npth,
                        subtract_mask_from_silk=subtract_mask_from_silk,
                        layers=use_layers, layer_plan=targets,
                        with_drill_map=(naming == "jlc"))

    if board is not None and not prefer_cli:
        files = export_kicad._kicad_plot(board, opt)
    else:
        files = export_kicad.export(board_path, opt, prefer_cli=prefer_cli)

    # 只收集本次 staging 内的产物，并检查非空
    files = [f for f in files if os.path.isfile(f)]
    map_files = [f for f in files if f.lower().endswith("-drl_map.gbr")]
    files = [f for f in files if f not in map_files]
    gdd_path = map_files[0] if map_files else None
    if gdd_path is None and naming == "jlc" and board is not None:
        # kicad-cli 后端不产钻孔图，用 pcbnew 补一张（嘉立创EDA 的 GDD）
        try:
            import tempfile

            import pcbnew as _pcbnew
            tmpd = tempfile.mkdtemp(prefix="drlmap_")
            w = _pcbnew.EXCELLON_WRITER(board)
            w.SetOptions(False, False, _pcbnew.VECTOR2I(0, 0), False)
            w.SetFormat(True, _pcbnew.EXCELLON_WRITER.DECIMAL_FORMAT)
            fmt = getattr(_pcbnew, "PLOT_FORMAT_GERBER", None)
            if fmt is not None:
                w.SetMapFileFormat(fmt)
            w.CreateDrillandMapFilesSet(tmpd, True, True)
            maps = [os.path.join(tmpd, f) for f in sorted(os.listdir(tmpd))
                    if f.lower().endswith("-drl_map.gbr")]
            if maps:
                gdd_path = maps[0]
        except Exception:                        # noqa: BLE001
            gdd_path = None
    empty = [os.path.basename(f) for f in files if os.path.getsize(f) == 0]
    result.files = files

    g = gate_mod.GateDecision()
    if src_copper is not None and len(copper_layers) != src_copper:
        g.add("导出（KiCad）", "failed",
              f"请求导出的铜层 {len(copper_layers)} 层（{copper_layers}）"
              f"与源板 {src_copper} 层不一致：多层板漏内层不得发布")
    if not files:
        g.add("导出（KiCad）", "failed", "没有产生任何制造文件")
    elif empty:
        g.add("导出（KiCad）", "failed", f"空文件：{empty}")
    else:
        g.add("导出（KiCad）", "passed", f"{len(files)} 个文件 → {staging}")

    # ---- 板级检查 ----
    if board is not None:
        try:
            result.board_issues = board_rules.check_board(board, rule_set)
        except Exception as exc:                     # noqa: BLE001
            result.board_issues = []
            g.add("板级几何检查", "failed", f"执行异常：{exc}")
        else:
            errs = [i for i in result.board_issues if i.level == "ERROR"]
            if errs:
                g.add("板级几何检查", "failed",
                      f"{len(errs)} 个 ERROR：" + "；".join(i.code for i in errs[:4]))
            else:
                g.add("板级几何检查", "passed",
                      f"{len(result.board_issues)} 条记录，无 ERROR")
    else:
        g.add("板级几何检查", "skipped", "无板子对象（只做文件级检查）")

    # ---- DRC（独立阶段）----
    drc = run_drc(board_path, refill_zones=True)
    result.drc_text = drc.get("text", "")
    if not drc.get("ran"):
        g.add("DRC", "skipped", drc.get("reason", "kicad-cli 不可用"),
              required=not allow_skip_drc)
    else:
        # 解析失败 = 结果不可信，直接阻断（过去会按返回码兜底成 0 违规）
        if drc.get("drc_parse_ok") is False:
            g.add("DRC", "failed", "DRC 报告解析失败，结果不可用于放行")
        by_type = dict(drc.get("by_type") or {})
        by_type, waived, problems = gate_mod.apply_drc_waivers(by_type, drc_waivers)
        for pr in problems:
            g.add("DRC 豁免", "failed", pr)
        remaining = sum(by_type.values())
        unconn = int(drc.get("unconnected") or 0)
        if remaining > 0:
            g.add("DRC", "failed", f"{remaining} 条 error 级违规：{sorted(by_type)}")
        if unconn > 0:
            # 未连接项不是"违规"字段的一部分，但同样意味着板子不能用
            ut = drc.get("unconnected_types") or {}
            detail = "、".join(f"{k}×{v}" for k, v in sorted(ut.items())) or f"{unconn} 项"
            g.add("DRC 连通性", "failed", f"仍有 {unconn} 个未连接项（{detail}）")
        if remaining > 0 or unconn > 0 or drc.get("drc_parse_ok") is False:
            pass
        elif waived:
            detail = "；".join(f"{k}×{v}" for k, v in sorted(waived.items()))
            g.add("DRC", "passed", f"0 条未豁免违规（人工豁免：{detail}），未连接 0")
            result.warnings.append(
                f"DRC 存在人工豁免：{detail}（已写入清单，请自行确认可接受）")
        else:
            g.add("DRC", "passed", "0 违规、未连接 0" + ("（已重铺铜）" if drc.get("refilled") else ""))

    # ---- 文件级预检 ----
    design = {"drill_expected": True}
    if src_copper:
        design["copper_layers"] = src_copper
    rep = preflight.check(files, board=name, rules=rule_set, design=design)
    result.report = rep
    if not rep.ok:
        g.add("文件级预检", "failed",
              f"{rep.count('ERROR')} 个 ERROR；未实现语法 {len(rep.unsupported)} 处")
    else:
        g.add("文件级预检", "passed",
              f"ERROR 0 / WARN {rep.count('WARN')} / INFO {rep.count('INFO')}")

    # ---- 飞针（可选）----
    if with_flying_probe and board is not None:
        p = flying_probe.write(board, staging)
        if p:
            files.append(p)
            g.add("飞针数据", "passed", os.path.basename(p))
        else:
            g.add("飞针数据", "skipped", "生成失败", required=False)
    elif with_flying_probe:
        g.add("飞针数据", "skipped", "无板子对象", required=False)

    result.gate = g
    if drc_waivers:
        g.warnings.append("DRC 豁免参数：" + str(drc_waivers))

    # 飞针 JSON 之类属于嘉立创EDA 也有的附件，跟着一起进包
    extras = [f for f in files if f.lower().endswith(".json")]

    if make_zip and naming == "jlc":
        via_text = None
        if board is not None:
            pts = []
            for tr in board.GetTracks():
                if isinstance(tr, pcbnew.PCB_VIA):
                    pp = tr.GetPosition()
                    pts.append((pp.x / 1e6, pp.y / 1e6, tr.GetDrillValue() / 1e6))
            via_text = native.via_drill_text(pts, version=easyeda_version,
                                             ts=easyeda_ts) if pts else None
        result.zip_path = _pack_jlc(files, targets, name, outdir, rep, g, extras,
                                    x2_hints=x2_hints,
                                    easyeda_version=easyeda_version,
                                    easyeda_ts=easyeda_ts,
                                    gdd_path=gdd_path, via_text=via_text)
    elif make_zip:
        zip_path = os.path.join(outdir, f"{name}_JLC.zip")
        try:
            info = package.build([f for f in files if os.path.isfile(f)], zip_path,
                                 board=name, report=rep, gate=g)
        except package.PackagingError as exc:
            g.add("打包", "failed", str(exc))
            result.zip_path = None
            result.warnings.append(f"打包失败：{exc}")
        else:
            g.add("打包", "passed", os.path.basename(info["zip"]))
            result.zip_path = info["zip"]
            result.warnings += info["warnings"]
    return result


def _pack_jlc(files: list[str], targets, name: str, outdir: str,
              rep, g: gate_mod.GateDecision, extras: list[str],
              x2_hints: bool = False,
              easyeda_version: str = native.EASYEDA_VERSION,
              easyeda_ts: str | None = None,
              gdd_path: str | None = None,
              via_text: str | None = None) -> str | None:
    """按嘉立创EDA 的组织方式打包：Gerber_<层名>.<后缀> + Drill_PTH_Through.DRL。"""
    by_tag = {}
    for f in files:
        base = os.path.basename(f)
        for t in targets:
            if f"-{t.tag}." in base:
                by_tag.setdefault(t.canonical, f)
                break

    drills = []
    for f in files:
        if not f.lower().endswith(".drl"):
            continue
        try:
            from . import excellon
            d = excellon.parse(f)
            plated = d.plated is not False
        except Exception:
            plated = "-npth" not in os.path.basename(f).lower()
        drills.append((f, plated))

    ext = []
    for e in extras:
        if os.path.basename(e).lower().endswith(".json"):
            ext.append((os.path.basename(e), e))

    want = f"Gerber_PCB_{name}_{__import__('datetime').date.today().isoformat()}.zip"
    out_zip = os.path.join(outdir, "x2" if x2_hints else "", want)
    if not g.publishable:
        out_zip = out_zip[:-4] + "_DIAGNOSTIC.zip"
    try:
        pkg = native.build(by_tag, drills, out_zip, board=name, report=rep,
                           extras=ext, x2_hints=x2_hints,
                           easyeda_version=easyeda_version, easyeda_ts=easyeda_ts,
                           gdd_path=gdd_path, via_text=via_text)
    except Exception as exc:                      # noqa: BLE001
        g.add("打包", "failed", str(exc))
        return None

    # 审计信息放在 zip **外面**（嘉立创EDA 的包里没有这类文件）
    audit = os.path.splitext(pkg.zip_path)[0] + "_report.json"
    with open(audit, "w", encoding="utf-8") as fh:
        import json
        json.dump({"package": pkg.report,
                   "gate": g.to_manifest(),
                   "preflight": rep.to_manifest() if rep is not None else None},
                  fh, ensure_ascii=False, indent=1)

    # zip 外的 sidecar：人读说明（内容与包内 readme 分离，避免破坏与嘉立创EDA 的一致性）
    side = os.path.splitext(pkg.zip_path)[0] + "_说明.txt"
    with open(side, "w", encoding="utf-8") as fh:
        fh.write(native.sidecar_text(name, pkg.report.get("generated_at", ""),
                                     pkg.entries, rep, g))

    g.add("打包", "passed", f"{os.path.basename(pkg.zip_path)}（{len(pkg.entries)} 个文件）")
    if not g.publishable:
        g.warnings.append(f"发布判定未通过：{os.path.basename(pkg.zip_path)} 仅供诊断，勿直接投板")
    return pkg.zip_path
