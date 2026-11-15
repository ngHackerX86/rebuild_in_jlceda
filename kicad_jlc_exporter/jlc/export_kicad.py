"""KiCad 侧导出后端：pcbnew 进程内出图，或转调 kicad-cli。

评审 S-02 / S-08 / S-10 之后的改动：

* `default_layers_for(board)` 从**实际启用铜层**生成层清单（不再写死双面）；
* `run_drc()` 调 `kicad-cli pcb drc --refill-zones`，作为独立检查阶段；
* CLI 路径发现包含 10.0 与 `JLC_KICAD_CLI` 环境变量；
* `--no-protel-ext`/`--no-protel-extension` 按实际 `--help` 探测，不猜；
* Gerber 与钻孔统一使用同一坐标系（绝对原点或钻孔原点，二选一）；
* 出图后核对"每层文件都存在且非空"，缺一个即报错，不静默放过。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class LayerTarget:
    """一层的导出计划：板上真实层名 / 规范层名 / 文件名标记。"""
    plot_name: str      # 板上实际层名（可能是 PWR、GND 这类自定义名）
    canonical: str      # F.Cu / In1.Cu / B.Cu / F.Mask ...
    tag: str            # 文件名用：F_Cu / In1_Cu / ...
    copper: bool = False


@dataclass
class ExportOptions:
    outdir: str
    board_name: str = ""
    use_protel_ext: bool = True        # 输出 .GTL/.GBL/... 便于嘉立创识别
    x2: bool = True                    # Gerber X2 属性（嘉立创忽略，但保留便于追溯）
    netlist_attrs: bool = True
    subtract_mask_from_silk: bool = False
    use_drill_origin: bool = False     # 用钻孔原点作坐标原点（嘉立创通常要绝对坐标）
    precision: int = 5                 # EasyEDA 用 4.5；KiCad 默认 4.6
    merge_pth_npth: bool = False
    with_drill_map: bool = False       # 额外出钻孔图（嘉立创EDA 的 GDD）
    layers: list[str] = field(default_factory=list)
    layer_plan: list[LayerTarget] = field(default_factory=list)


# 与 kicad 层名对应的输出后缀（仅用于"出图后核对是否齐全"）
_EXPECTED_EXT = {
    "F.Cu": ".gtl", "B.Cu": ".gbl", "F.SilkS": ".gto", "B.SilkS": ".gbo",
    "F.Mask": ".gts", "B.Mask": ".gbs", "F.Paste": ".gtp", "B.Paste": ".gbp",
    "Edge.Cuts": ".gm1",
}


def plan_layers(board, with_doc_layer: bool = False) -> list[LayerTarget]:
    """按**层 id**枚举要导出的层，层被改名也不影响。

    铜层：F.Cu(0) / 内层(1..30) / B.Cu(31)，规范名一律用 In<n>.Cu；
    工艺层：丝印、阻焊、钢网、板框（用板上的真实层名去画）。
    """
    import pcbnew

    targets: list[LayerTarget] = []

    def add(lid: int, canonical: str, tag: str, copper: bool):
        if not board.IsLayerEnabled(lid):
            return
        targets.append(LayerTarget(board.GetLayerName(lid), canonical, tag, copper))

    add(pcbnew.F_Cu, "F.Cu", "F_Cu", True)
    # 内层按 id 升序重新编号：KiCad 允许把内层放在 id 4/6 这类位置
    # （openair-max 的 PWR/GND 就在 4/6），而嘉立创要的是"叠层顺序"G1/G2。
    n = 0
    for lid in range(1, 31):
        if lid == pcbnew.B_Cu:
            continue
        if board.IsLayerEnabled(lid) and pcbnew.IsCopperLayer(lid):
            n += 1
            targets.append(LayerTarget(board.GetLayerName(lid), f"In{n}.Cu",
                                       f"In{n}_Cu", True))
    add(pcbnew.B_Cu, "B.Cu", "B_Cu", True)
    add(pcbnew.F_SilkS, "F.SilkS", "F_SilkS", False)
    add(pcbnew.B_SilkS, "B.SilkS", "B_SilkS", False)
    add(pcbnew.F_Mask, "F.Mask", "F_Mask", False)
    add(pcbnew.B_Mask, "B.Mask", "B_Mask", False)
    add(pcbnew.F_Paste, "F.Paste", "F_Paste", False)
    add(pcbnew.B_Paste, "B.Paste", "B_Paste", False)
    add(pcbnew.Edge_Cuts, "Edge.Cuts", "Edge_Cuts", False)
    if with_doc_layer and board.IsLayerEnabled(pcbnew.F_Fab):
        # 嘉立创EDA 的 Gerber_DocumentLayer.GDL 对应"文档/装配"层，这里用 F.Fab
        targets.append(LayerTarget(board.GetLayerName(pcbnew.F_Fab), "F.Fab", "F_Fab", False))
    return targets


_CANON = {
    "F.Cu": "F.Cu", "B.Cu": "B.Cu", "F.SilkS": "F.SilkS", "B.SilkS": "B.SilkS",
    "F.Silkscreen": "F.SilkS", "B.Silkscreen": "B.SilkS",
    "F.Mask": "F.Mask", "B.Mask": "B.Mask", "F.Paste": "F.Paste",
    "B.Paste": "B.Paste", "Edge.Cuts": "Edge.Cuts",
}


def targets_from_names(names: list[str]) -> list[LayerTarget]:
    """没有板子对象时：按名字构造计划（内层改名的情况无法推断，只能靠调用方给规范名）。"""
    out: list[LayerTarget] = []
    for n in names:
        canon = _CANON.get(n, n)
        copper = canon.endswith(".Cu")
        out.append(LayerTarget(n, canon, canon.replace(".", "_"), copper))
    return out


def default_layers_for(board) -> list[str]:
    """兼容旧接口：返回规范层名列表。"""
    return [t.canonical for t in plan_layers(board)]


def _kicad_plot(board, opt: ExportOptions) -> list[str]:
    import pcbnew  # 仅在 KiCad 进程内可用

    os.makedirs(opt.outdir, exist_ok=True)
    # 出图前重铺铜：避免用陈旧填充出图（不改磁盘上的设计）
    try:
        filler = pcbnew.ZONE_FILLER(board)
        filler.Fill(board.Zones())
    except Exception as exc:                       # noqa: BLE001
        raise RuntimeError(f"重铺铜失败，拒绝在未知填充状态下出图：{exc}")
    pc = pcbnew.PLOT_CONTROLLER(board)
    po = pc.GetPlotOptions()

    def setb(name, value, critical=False):
        fn = getattr(po, name, None)
        if callable(fn):
            fn(value)
        elif critical:
            raise RuntimeError(f"KiCad 版本不兼容：{name} 不存在，无法保证出图参数")

    setb("SetOutputDirectory", opt.outdir, critical=True)
    setb("SetPlotFrameRef", False)
    setb("SetPlotValue", True)
    setb("SetPlotReference", True)
    setb("SetUseGerberProtelExtensions", opt.use_protel_ext)
    setb("SetUseGerberX2format", opt.x2)
    setb("SetIncludeGerberNetlistInfo", opt.netlist_attrs)
    setb("SetSubtractMaskFromSilk", opt.subtract_mask_from_silk)
    setb("SetUseAuxOrigin", opt.use_drill_origin)
    setb("SetGerberPrecision", opt.precision)
    setb("SetAutoScale", False)
    setb("SetScale", 1.0)
    setb("SetMirror", False)
    setb("SetDrillMarksType", 0)
    setb("SetCreateGerberJobFile", False)   # job file 嘉立创不解析，省掉
    setb("SetSkipPlotNPTH_Pads", False)

    # KiCad 7/8/9/10 的 SWIG 把枚举暴露成扁平常量 PLOT_FORMAT_GERBER；
    # 少数版本/绑定里是 PLOT_FORMAT.GERBER。两者都试，都不在就报错（不猜）。
    plot_fmt = getattr(pcbnew, "PLOT_FORMAT_GERBER", None)
    if plot_fmt is None:
        fmt = getattr(pcbnew, "PLOT_FORMAT", None)
        plot_fmt = getattr(fmt, "GERBER", None) if fmt is not None else None
    if plot_fmt is None:
        raise RuntimeError("KiCad 版本不兼容：找不到 PLOT_FORMAT_GERBER / PLOT_FORMAT.GERBER")

    plan = opt.layer_plan or targets_from_names(opt.layers)
    pending: list[tuple[str, str]] = []          # (绘图器写出的路径, 目标路径)
    for t in plan:
        lid = board.GetLayerID(t.plot_name)
        if lid < 0 or not board.IsLayerEnabled(lid):
            raise RuntimeError(f"层 {t.plot_name}（{t.canonical}）不可用，拒绝出图")
        pc.SetLayer(lid)
        pc.OpenPlotfile(t.plot_name, plot_fmt, t.plot_name)
        pc.PlotLayer()
        src = str(pc.GetPlotFileName())
        # 统一改名：层被改成 PWR/GND 也能得到 <板名>-In1_Cu.<原扩展名>
        stem, ext = os.path.splitext(src)
        dst = os.path.join(opt.outdir, f"{opt.board_name or 'board'}-{t.tag}{ext}")
        pending.append((src, dst))
    pc.ClosePlot()                               # 关闭后才允许改名/移动

    written: list[str] = []
    for src, dst in pending:
        if os.path.abspath(src) != os.path.abspath(dst):
            os.replace(src, dst)
        written.append(dst)

    # 钻孔：坐标原点与 Gerber 保持一致
    writer = pcbnew.EXCELLON_WRITER(board)
    aux = pcbnew.VECTOR2I(0, 0)
    if opt.use_drill_origin:
        try:
            aux = board.GetDesignSettings().GetAuxOrigin()
        except Exception:
            aux = pcbnew.VECTOR2I(0, 0)
    writer.SetOptions(False, False, aux, opt.merge_pth_npth)
    writer.SetFormat(True, pcbnew.EXCELLON_WRITER.DECIMAL_FORMAT)
    if opt.with_drill_map:
        try:
            fmt = getattr(pcbnew, "PLOT_FORMAT_GERBER", None)
            if fmt is not None:
                writer.SetMapFileFormat(fmt)
        except Exception:                        # noqa: BLE001
            pass
    writer.CreateDrillandMapFilesSet(opt.outdir, True, bool(opt.with_drill_map))

    for f in sorted(os.listdir(opt.outdir)):
        low = f.lower()
        if low.endswith(".drl"):
            written.append(os.path.join(opt.outdir, f))
        elif opt.with_drill_map and low.endswith("-drl_map.gbr"):
            written.append(os.path.join(opt.outdir, f))

    _verify_outputs(written, opt)
    return written


def _verify_outputs(files: list[str], opt: ExportOptions):
    """出图后核对：文件存在、非空、铜层数量对得上。"""
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise RuntimeError(f"出图缺少文件：{missing}")
    empty = [f for f in files if os.path.getsize(f) == 0]
    if empty:
        raise RuntimeError(f"出图为空文件：{empty}")
    plan = opt.layer_plan or targets_from_names(opt.layers)
    copper = [t for t in plan if t.copper]
    produced = [f for f in files
                if any(f"-{t.tag}." in f.replace("\\", "/") for t in plan)]
    if copper and len(produced) < len(plan):
        raise RuntimeError(
            f"出图数量不符：期望 {len(plan)} 层（含 {len(copper)} 个铜层），"
            f"实际只有 {len(produced)} 个；拒绝继续")


def find_kicad_cli() -> str | None:
    env = os.environ.get("JLC_KICAD_CLI")
    if env and os.path.isfile(env):
        return env
    exe = shutil.which("kicad-cli")
    if exe:
        return exe
    cands: list[str] = []
    # Windows：优先读注册表的 InstallLocation（KiCad 允许装到任意盘）
    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                        try:
                            root = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view)
                        except OSError:
                            continue
                        for i in range(winreg.QueryInfoKey(root)[0]):
                            try:
                                name = winreg.EnumKey(root, i)
                                with winreg.OpenKey(root, name) as k:
                                    disp = winreg.QueryValueEx(k, "DisplayName")[0]
                                    if "KiCad" not in str(disp):
                                        continue
                                    try:
                                        loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                                    except OSError:
                                        continue
                                    cands.append(os.path.join(str(loc), "bin", "kicad-cli.exe"))
                            except OSError:
                                continue
        except Exception:
            pass
    for ver in ("10.0", "9.0", "8.0", "7.0", "11.0"):
        cands += [rf"C:\Program Files\KiCad\{ver}\bin\kicad-cli.exe",
                  rf"C:\Program Files (x86)\KiCad\{ver}\bin\kicad-cli.exe"]
    cands += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


_NO_PROTEL_FLAG: str | None = None


def _protel_flag(cli: str) -> str:
    """按实际 --help 探测"关闭 Protel 扩展名"的参数名（10.0 文档写作 --no-protel-ext）。"""
    global _NO_PROTEL_FLAG
    if _NO_PROTEL_FLAG is not None:
        return _NO_PROTEL_FLAG
    _NO_PROTEL_FLAG = "--no-protel-extension"        # 7/8/9 的写法
    try:
        p = subprocess.run([cli, "pcb", "export", "gerbers", "-h"],
                           capture_output=True, text=True, timeout=40)
        text = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"--no-protel-ext(?:ension)?", text)
        if m:
            _NO_PROTEL_FLAG = m.group(0)
    except Exception:
        pass
    return _NO_PROTEL_FLAG


def export_via_cli(board_path: str, opt: ExportOptions, kicad_cli: str | None = None) -> list[str]:
    cli = kicad_cli or find_kicad_cli()
    if not cli:
        raise FileNotFoundError(
            "找不到 kicad-cli：可用 JLC_KICAD_CLI 环境变量显式指定，"
            "或在 KiCad 内使用进程内导出")
    os.makedirs(opt.outdir, exist_ok=True)
    layers = ",".join(opt.layers)
    cmd = [cli, "pcb", "export", "gerbers", "-o", opt.outdir, "-l", layers]
    if not opt.use_protel_ext:
        cmd.append(_protel_flag(cli))
    if not opt.x2:
        cmd.append("--no-x2")
    if not opt.netlist_attrs:
        cmd.append("--no-netlist")
    if opt.subtract_mask_from_silk:
        cmd.append("--subtract-soldermask")
    if opt.use_drill_origin:
        cmd.append("--use-drill-file-origin")
    cmd += ["--precision", str(opt.precision)]
    cmd.append(board_path)

    drill = [cli, "pcb", "export", "drill", "-o", opt.outdir,
             "--format", "excellon", "--excellon-units", "mm",
             "--excellon-zeros-format", "decimal",
             "--drill-origin", "plot" if opt.use_drill_origin else "absolute"]
    if not opt.merge_pth_npth:
        drill.append("--excellon-separate-th")
    drill.append(board_path)

    for c in (cmd, drill):
        p = subprocess.run(c, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"{' '.join(c)}\n{p.stdout}\n{p.stderr}")

    out = _normalize_cli_outputs(opt)
    _verify_outputs(out, opt)
    return out


def _sanitize(name: str) -> str:
    return name.replace(".", "_").replace(" ", "_")


def _normalize_cli_outputs(opt: ExportOptions) -> list[str]:
    """kicad-cli 用"层名"给文件起名（PWR→xxx-PWR.gbr），这里统一改成规范名。"""
    plan = opt.layer_plan or targets_from_names(opt.layers)
    files = [os.path.join(opt.outdir, f) for f in sorted(os.listdir(opt.outdir))]
    out: list[str] = []
    for t in plan:
        want = f"-{_sanitize(t.plot_name)}."
        hit = [f for f in files if want in os.path.basename(f)]
        if not hit:
            raise RuntimeError(f"kicad-cli 没有产出 {t.plot_name}（{t.canonical}）的文件")
        src = hit[0]
        stem, ext = os.path.splitext(src)
        dst = os.path.join(opt.outdir, f"{opt.board_name or 'board'}-{t.tag}{ext}")
        if os.path.abspath(src) != os.path.abspath(dst):
            os.replace(src, dst)
        out.append(dst)
    # 钻孔与 job 文件原样带上
    for f in files:
        base = os.path.basename(f)
        if base.lower().endswith((".drl", ".gbrjob")) and f not in out:
            out.append(f)
    return out


def run_drc(board_path: str, refill_zones: bool = True) -> dict:
    """独立 DRC 阶段。返回 {ran, violations, errors, types, refilled, text, reason}。

    用 `--format json` 解析：文本报告会随界面语言本地化（中文版写"发现 2 条违规项"），
    靠正则数数不可靠；JSON 里的 `violations[].severity` 才是稳定依据。
    """
    cli = find_kicad_cli()
    if not cli:
        return {"ran": False, "violations": None, "types": [], "text": "",
                "reason": "找不到 kicad-cli，无法执行 DRC"}
    out_json = os.path.join(os.path.dirname(os.path.abspath(board_path)),
                            os.path.basename(board_path).replace(".kicad_pcb", "-drc.json"))
    cmd = [cli, "pcb", "drc", "--format", "json", "-o", out_json,
           "--units", "mm", "--severity-error", "--exit-code-violations"]
    if refill_zones:
        cmd += ["--refill-zones"]
    cmd.append(board_path)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as exc:                          # noqa: BLE001
        return {"ran": False, "violations": None, "types": [], "text": "",
                "reason": f"DRC 执行失败：{exc}"}

    text = (p.stdout or "") + (p.stderr or "")
    errors, types, by_type = None, [], {}
    try:
        import json
        with open(out_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("violations") or []
        err_items = [v for v in items if (v.get("severity") or "").lower() == "error"]
        errors = len(err_items)
        types = sorted({v.get("type", "?") for v in err_items})
        for v in err_items:
            by_type[v.get("type", "?")] = by_type.get(v.get("type", "?"), 0) + 1
        # 未连接项单独统计，便于区分"没布线"和"违反规则"
        unconn = len(data.get("unconnected_items") or [])
        unconn_types: dict[str, int] = {}
        for _u in (data.get("unconnected_items") or []):
            _k = _u.get("type") or "unconnected"
            unconn_types[_k] = unconn_types.get(_k, 0) + 1
        text += f"\n[DRC] 违规 {len(items)} 条（error {errors}），未连接 {unconn} 项"
        parse_ok = True
    except Exception as exc:
        # 解析失败不能当成"0 违规"：这份报告不可信，必须让 Gate 看见
        errors = 0 if p.returncode == 0 else 1
        unconn, unconn_types = 0, {}
        parse_ok = False
        text += f"\n[DRC] JSON 报告解析失败（{exc.__class__.__name__}），该结果不可用于放行"
    return {"ran": True, "violations": errors, "types": types, "by_type": by_type,
            "unconnected": unconn, "unconnected_types": unconn_types,
            "drc_parse_ok": parse_ok,
            "refilled": refill_zones, "text": text[-4000:],
            "reason": "" if parse_ok else "drc_report_unparsable"}


def export(board_path: str, opt: ExportOptions, prefer_cli: bool = False) -> list[str]:
    """统一入口：优先进程内 pcbnew，失败则退到 kicad-cli。"""
    if not prefer_cli:
        try:
            import pcbnew
            board = pcbnew.LoadBoard(board_path)
            return _kicad_plot(board, opt)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[jlc] pcbnew 导出失败，改走 kicad-cli：{exc}\n")
    return export_via_cli(board_path, opt)
