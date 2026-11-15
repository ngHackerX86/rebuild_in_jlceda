"""命令行入口：

  # 只检查一个已有 Gerber 包（zip 或目录），不装 KiCad 也能用
  python -m jlc.cli check Gerber_PCB_xxx.zip

  # 完整流程：KiCad 板 -> Gerber -> 预检 -> DRC -> 发布判定 -> 嘉立创 zip
  python -m jlc.cli export board.kicad_pcb -o out --name MYBOARD

  # 把已经导出的 Gerber 目录重新打包成嘉立创 zip
  python -m jlc.cli package gerber/ -o MYBOARD_JLC.zip --name MYBOARD

退出码：0 = 可发布 / 预检通过；1 = 预检有 ERROR 或打包冲突；
        2 = 有必需阶段未执行（仅当未加 --allow-incomplete）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import zipfile


def _iter_manufacturing_files(root: str) -> list[str]:
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            out.append(os.path.join(dirpath, n))
    return out


def _name_from_path(src: str) -> str:
    """从 zip / 目录名猜板名：Gerber_PCB_USS_X_2026-04-27.zip -> USS_X"""
    base = os.path.basename(os.path.abspath(src))
    if base.lower().endswith(".zip"):
        base = base[:-4]
    for pre in ("Gerber_PCB_", "Gerber_", "PCB_"):
        if base.startswith(pre):
            base = base[len(pre):]
    base = re.sub(r"[_\-]?\d{4}[-_]\d{2}[-_]\d{2}$", "", base)
    return base.strip("_- ") or "PCB"


def _unpack(src: str) -> tuple[str, tempfile.TemporaryDirectory | None]:
    if os.path.isdir(src):
        return src, None
    if src.lower().endswith(".zip"):
        td = tempfile.TemporaryDirectory(prefix="jlc_")
        with zipfile.ZipFile(src) as z:
            z.extractall(td.name)
        return td.name, td
    raise SystemExit(f"不支持的输入：{src}（给 zip 或目录）")


def _gate_for_files(rep) -> object:
    """纯检查场景的发布判定：只含文件级预检一个阶段。"""
    from . import gate as gate_mod

    g = gate_mod.GateDecision()
    if rep.unsupported:
        g.add("解析完整性", "failed",
              "；".join(f"{k}: {'; '.join(v)}" for k, v in rep.unsupported.items()))
    else:
        g.add("解析完整性", "passed", "全部语法已实现")
    if rep.ok:
        g.add("文件级预检", "passed",
              f"ERROR 0 / WARN {rep.count('WARN')} / INFO {rep.count('INFO')}")
    else:
        g.add("文件级预检", "failed", f"{rep.count('ERROR')} 个 ERROR")
    g.add("DRC/板级检查", "skipped", "纯文件检查模式未执行", required=False)
    return g


def cmd_check(args) -> int:
    from . import preflight
    root, td = _unpack(args.path)
    files = _iter_manufacturing_files(root)
    rules = preflight.load_rules(args.rules)
    rep = preflight.check(files, board=args.name or _name_from_path(args.path), rules=rules)
    g = _gate_for_files(rep)
    text = rep.to_text() + "\n" + g.to_text()
    print(text)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    if td:
        td.cleanup()
    return 0 if rep.ok else 1


def cmd_package(args) -> int:
    from . import package, preflight
    root, td = _unpack(args.path)
    files = _iter_manufacturing_files(root)
    rules = preflight.load_rules(args.rules)
    name = args.name or _name_from_path(args.path)
    rep = preflight.check(files, board=name, rules=rules)
    g = _gate_for_files(rep)
    print(rep.to_text())
    print(g.to_text())
    if not g.release:
        print("\n发布判定未通过：不生成正式包（如确需诊断，请先修复 ERROR）")
        if td:
            td.cleanup()
        return 1
    if args.naming == "jlc":
        zip_path = _package_jlc(files, name, args.out, rep)
        if zip_path is None:
            if td:
                td.cleanup()
            return 1
        print(f"\n成品：{zip_path}")
        if td:
            td.cleanup()
        return 0

    try:
        info = package.build(files, args.out, board=name, report=rep, gate=g)
    except package.PackagingError as exc:
        print(f"\n打包冲突，未生成正式包：\n  {exc}")
        if td:
            td.cleanup()
        return 1
    for w in info["warnings"]:
        print("  [打包]", w)
    print(f"\n成品：{info['zip']}  ({info['size']} 字节)")
    if td:
        td.cleanup()
    return 0


def _package_jlc(files: list[str], name: str, out: str, rep) -> str | None:
    """按嘉立创EDA 组织方式重打包一批已有文件（不改内容、不动出处）。"""
    import os

    from . import excellon, layers, native  # noqa: F401

    by_canonical: dict[str, str] = {}
    drills: list[tuple[str, bool]] = []
    extras: list[tuple[str, str]] = []
    for f in files:
        ki, _why = layers.classify(f)
        # 文档层（GDL）在嘉立创EDA 包里也有，重打包时一并保留
        if ki is not None and (ki.kind != "other" or native.easyeda_name(ki.kicad)):
            by_canonical[ki.kicad] = f
            continue
        if os.path.basename(f).upper().endswith(".GDD"):
            extras.append((os.path.basename(f), f))     # 钻孔图表：原样带上
            continue
        if f.lower().endswith(".drl"):
            d = excellon.parse(f)
            drills.append((f, d.plated is not False))
            continue
        if f.lower().endswith(".json") and "FlyingProbe" in os.path.basename(f):
            extras.append((os.path.basename(f), f))
            continue
        print(f"  [跳过] {os.path.basename(f)} 不是制造层")
    if not by_canonical:
        print("没有可用的制造层，放弃打包")
        return None
    if not out.lower().endswith(".zip"):
        import datetime as _dt
        out = os.path.join(out, f"Gerber_PCB_{name}_{_dt.date.today().isoformat()}.zip")
    pkg = native.build(by_canonical, drills, out, board=name, report=rep, extras=extras)
    return pkg.zip_path


def _parse_waivers(items: list[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items or []:
        if ":" not in it:
            raise SystemExit(f"--drc-waiver 需要 TYPE:COUNT 形式，收到 {it!r}")
        kind, cnt = it.rsplit(":", 1)
        out[kind.strip()] = int(cnt)
    return out


def cmd_export(args) -> int:
    from . import pipeline
    try:
        res = pipeline.run(args.board, args.out, board_name=args.name,
                           layers=([l.strip() for l in args.layers.split(",")]
                                   if args.layers else None),
                           make_zip=not args.no_zip,
                           merge_pth_npth=args.merge_drills,
                           subtract_mask_from_silk=not args.keep_silk_over_pad,
                           with_flying_probe=args.flying_probe,
                           prefer_cli=args.use_cli,
                           rules_path=args.rules,
                           allow_skip_drc=args.allow_incomplete,
                           drc_waivers=_parse_waivers(args.drc_waiver),
                           naming=args.naming, x2_hints=args.x2_hints,
                           with_doc_layer=args.with_doc_layer,
                           easyeda_version=args.easyeda_version,
                           easyeda_ts=args.easyeda_time)
    except (ValueError, FileNotFoundError) as exc:
        print(f"导出失败：{exc}")
        return 1
    print(res.to_text())
    if res.gate is None:
        return 1
    return 0 if res.gate.publishable else (2 if res.gate.release else 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jlc", description="KiCad → 嘉立创 出图/预检/打包")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="预检已有 Gerber 包")
    c.add_argument("path")
    c.add_argument("--name", default="")
    c.add_argument("--rules", default=None, help="JSON 规则文件（覆盖默认阈值）")
    c.add_argument("--out", default=None, help="把报告另存为文本")
    c.set_defaults(func=cmd_check)

    p = sub.add_parser("package", help="把 Gerber 目录重命名并打包成嘉立创 zip")
    p.add_argument("path")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--rules", default=None)
    p.add_argument("--naming", choices=["jlc", "generic"], default="jlc",
                   help="jlc=与嘉立创EDA 一致（默认，且保留原有出处行）")
    p.set_defaults(func=cmd_package)

    e = sub.add_parser("export", help="从 .kicad_pcb 直接出图并打包")
    e.add_argument("board")
    e.add_argument("-o", "--out", default=None, help="输出目录")
    e.add_argument("--name", default=None, help="板名（决定文件名前缀）")
    e.add_argument("--layers", default=None,
                   help="显式层清单（无 pcbnew 时必需），逗号分隔")
    e.add_argument("--no-zip", action="store_true")
    e.add_argument("--merge-drills", action="store_true", help="PTH/NPTH 合并为一个钻孔文件")
    e.add_argument("--keep-silk-over-pad", action="store_true", help="不从丝印中挖掉阻焊开窗")
    e.add_argument("--flying-probe", dest="flying_probe", action="store_true", default=None,
                   help="生成 FlyingProbeTesting.json（jlc 命名下默认开启）")
    e.add_argument("--no-flying-probe", dest="flying_probe", action="store_false",
                   help="不生成 FlyingProbeTesting.json")
    e.add_argument("--use-cli", action="store_true", help="强制走 kicad-cli 后端")
    e.add_argument("--rules", default=None, help="JSON 规则文件")
    e.add_argument("--easyeda-version", default="3.2.149.88089769",
                   help="写进头部/钻孔头的 EasyEDA 版本号（默认与参考样本一致）")
    e.add_argument("--easyeda-time", default=None,
                   help="写进头部的导出时间 YYYY-MM-DD HH:MM:SS（默认当前时间）")
    e.add_argument("--with-doc-layer", dest="with_doc_layer", action="store_true", default=None,
                   help="输出 Gerber_DocumentLayer.GDL（jlc 命名下默认开启）")
    e.add_argument("--no-doc-layer", dest="with_doc_layer", action="store_false",
                   help="不输出 Gerber_DocumentLayer.GDL")
    e.add_argument("--x2-hints", action="store_true",
                   help="额外输出一份带 %%TF.FileFunction 层功能属性的包（放在 x2/ 子目录），"
                        "供只认 X2 属性的解析器使用；默认包与嘉立创EDA 完全一致")
    e.add_argument("--naming", choices=["jlc", "generic"], default="jlc",
                   help="打包命名：jlc=与嘉立创EDA 一致（Gerber_<层名>.<后缀>）；"
                        "generic=<板名>.GTL 等（默认 jlc）")
    e.add_argument("--drc-waiver", action="append", default=None, metavar="TYPE:COUNT",
                   help="人工豁免某类 DRC 违规，必须给出精确条数；可重复，"
                        "例如 --drc-waiver drill_out_of_range:199（会写入清单）")
    e.add_argument("--allow-incomplete", action="store_true",
                   help="允许 DRC 未执行（会写进清单，属明确的人工例外）")
    e.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
