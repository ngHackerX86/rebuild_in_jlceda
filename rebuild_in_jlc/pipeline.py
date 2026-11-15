# -*- coding: utf-8 -*-
"""重建流水线：插件与命令行共用同一套实现。

流程（每一步都有独立状态与日志）：
    1. convert     KiCad .kicad_pcb → .eprj3
    2. to_epro2    .eprj3 → .epro2（用 EDA 自己导出，保证原生）
    3. import      .epro2 → 账号工作区，回读 uuid
    4. open        打开工程 + PCB
    5. rules       对齐间距类规则
    6. repour      人工确认「重建覆铜」已完成（API 无法触发）
    7. drc         DRC 复测
    8. export      从账号工程导出 Gerber/钻孔包
    9. compare     与 KiCad 参考导出逐层对照

命令行用法（headless，便于 CI 与自测）：
    python -m rebuild_in_jlc.pipeline --board x.kicad_pcb --name X --out out/run
    python -m rebuild_in_jlc.pipeline --board x.kicad_pcb --name X --out out/run --stop-after rules
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from . import console

STEP_TITLES = {
    "convert": "① 生成 .eprj3",
    "to_epro2": "② 打包 .epro2",
    "import": "③ 导入账号工作区",
    "open": "④ 打开工程与 PCB",
    "knockout": "④b 补写反相文字",
    "rules": "⑤ 对齐设计规则",
    "repour": "⑥ 重建覆铜（自动，失败回退人工）",
    "drc": "⑦ DRC 复测",
    "export": "⑧ 导出 Gerber 包",
    "compare": "⑨ 与 KiCad 逐层对照",
}


class StepResult(dict):
    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900,
         env_extra: dict | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items() if v})
    console.cmd(cmd)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, env=env,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    console.result(proc.returncode, time.time() - t0, out)
    return proc.returncode, out


def _python_for_board(cfg: dict) -> str:
    """读 KiCad 板要用带 pcbnew 的 python；实在没有就退回当前解释器。"""
    candidate = cfg.get("kicad_python") or ""
    return candidate if candidate and Path(candidate).exists() else sys.executable


_KICAD_HOSTS = {"pcbnew", "kicad", "eeschema", "pcbnew_do", "kicad-cli"}


def _py_for_eda(cfg: dict) -> str:
    """挑一个**普通 python** 来跑 tools/ 下的脚本。

    注意：在 KiCad GUI 里 `sys.executable` 是 **pcbnew.exe**（内嵌解释器宿主），
    拿它去跑脚本会让 pcbnew 去解析我们的命令行参数，弹 "Unknown option ..." 之类的错误框。
    因此优先用配置里的 kicad_python，其次找一个真正的 python.exe，最后才退回 sys.executable。
    """
    cand = (cfg.get("kicad_python") or "").strip()
    if cand and Path(cand).exists() and Path(cand).stem.lower().startswith("python"):
        return cand
    exe = sys.executable
    if Path(exe).stem.lower() in _KICAD_HOSTS:
        for name in ("python", "python3", "py"):
            hit = shutil.which(name)
            if hit and Path(hit).stem.lower().startswith(("python", "py")):
                return hit
    return exe


# --------------------------------------------------------------------------- 步骤
def step_convert(cfg: dict, board: Path, work: Path, name: str) -> StepResult:
    py = _python_for_board(cfg)
    out = work / "build"
    code, log = _run([py, str(Path(cfg["repo_root"]) / "tools" / "kicad2eprj3.py"),
                      str(board), "-o", str(out), "--name", name,
                      "--template", cfg["template"]], timeout=1800)
    proj = out / f"{name}_eprj3" / f"{name}.eprj3"
    return StepResult(ok=code == 0 and proj.exists(), log=log, project=str(proj))


def step_to_epro2(cfg: dict, project: str, work: Path) -> StepResult:
    epro2 = work / "rebuild.epro2"
    code, log = _run([_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "eda_export_epro2.py"),
                      project, "-o", str(epro2), "--exe", cfg["eda_exe"]], timeout=1800)
    return StepResult(ok=code == 0 and epro2.exists(), log=log, epro2=str(epro2))


def step_import(cfg: dict, epro2: str, work: Path, proj_name: str | None = None) -> StepResult:
    # 工程名末尾带"年月日_时分秒"时间戳，方便在 EDA 里一眼分辨是哪次导入
    name = proj_name or time.strftime("KiCad_%Y%m%d_%H%M%S")
    code, log = _run([_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "eda_import_verify.py"),
                      epro2, "--proj-name", name], timeout=1800,
                     env_extra={"JLC_EDA_EXE": cfg["eda_exe"]})
    uuid = None
    for line in log.splitlines():
        line = line.strip()
        if line.startswith("{") and '"imported"' in line:
            try:
                data = json.loads(line)
                uuid = ((data.get("value") or {}).get("imported") or {}).get("uuid")
            except Exception:
                pass
    return StepResult(ok=bool(uuid), log=log, uuid=uuid)


def step_open(cfg: dict, uuid: str) -> StepResult:
    code, log = _run([_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "eda_open_project.py"),
                      uuid, "--expect-uuid", uuid], timeout=1800,
                     env_extra={"JLC_EDA_EXE": cfg["eda_exe"]})
    ok = code == 0 and '"opened": true' in log.replace('"opened":true', '"opened": true') \
        and '"uuidMatch": true' in log.replace('"uuidMatch":true', '"uuidMatch": true')
    return StepResult(ok=ok, log=log)


def step_knockout(cfg: dict, work: Path, name: str) -> StepResult:
    """补写反显文字的"反相"标志。

    KiCad 的 knockout 文字会写成 STRING 的 reverse=true，但 EDA **导入时丢掉**这个标志
    （属性面板显示"反相=否"），必须导入后用 API modify 再写一次。
    转换器会把清单写到 <build>/<name>_eprj3/knockout_texts.json。
    """
    sidecar = work / "build" / f"{name}_eprj3" / "knockout_texts.json"
    if not sidecar.exists():
        return StepResult(ok=True, status="skipped", log="没有反显文字清单，跳过")
    code, log = _run([_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "eda_apply_knockout.py"),
                      str(sidecar), "--exe", cfg["eda_exe"]], timeout=600,
                     env_extra={"JLC_EDA_EXE": cfg["eda_exe"]})
    return StepResult(ok=code == 0, log=log)


def step_rules(cfg: dict) -> StepResult:
    code, log = _run([_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "eda_rules.py"),
                      "--exe", cfg["eda_exe"], "apply", "--profile", cfg.get("rules_profile", "kicad-min")],
                     timeout=1800)
    return StepResult(ok=code == 0, log=log)


REPOUR_STATUS = {
    "ok": "auto",
    "unsupported_api": "unsupported",
    "no_pour": "no_pour",
    "not_needed": "not_needed",
    "no_document": "no_document",
    "no_session": "no_session",
    "rejected": "rejected",
    "timeout": "rejected",
    "env": "env_error",
}

# 每种失败给一句可执行的下一步（GUI 会原样显示）
REPOUR_NEXT = {
    "unsupported": "当前编辑器版本没有 rebuildCopperRegion 接口：请在 EDA 手工执行"
                   "「设计 → 重建覆铜」(Shift+B)，再用 --repour-confirmed 继续。",
    "no_pour": "当前 PCB 没有铺铜区域：确认源板有铺铜，或检查导入是否完整。",
    "no_document": "没有打开 PCB 文档：先打开该工程的 PCB 页，或跑 tools/eda_open_project.py。",
    "not_needed": "本板没有铺铜区，无需重铺（属正常情况）。",
    "no_session": "没有在线会话：打开嘉立创EDA 并登录，至少打开一个工程。",
    "rejected": "接口可调用但没产生填充：在 EDA 里手工 Shift+B 复核，或看 DRC 的覆铜告警。",
    "env_error": "核对 JLC_EDA_EXE 与 EDA 安装路径。",
}


def step_repour(cfg: dict | None = None, auto: bool = True,
                manual_confirmed: bool = False, log: str = "") -> StepResult:
    """重建覆铜。

    自动路径：对每块 POUR 调 `rebuildCopperRegion()`（等价于人手的 Shift+B）。
    返回值带 `status`，区分六类失败（接口缺失 / 无铺铜 / 无 PCB 文档 / 无会话 /
    编辑器拒绝 / 环境错误），并给出可执行的下一步 `next`。

    人工路径：manual_confirmed=True 表示用户已在 EDA 里手工重铺。
    """
    if manual_confirmed:
        return StepResult(ok=True, status="manual", auto=False,
                          log=log or "人工确认：已在 EDA 中执行「设计 → 重建覆铜」")

    if not (auto and cfg):
        return StepResult(ok=False, status="needs_manual", auto=False,
                          next="已禁用自动重铺：请在 EDA 手工执行「设计 → 重建覆铜」后 "
                               "用 --repour-confirmed 继续",
                          log="已禁用自动重铺")

    script = Path(cfg["repo_root"]) / "tools" / "eda_repour.py"
    code, out = _run([_py_for_eda(cfg), str(script), "--exe", cfg["eda_exe"], "--json"],
                     timeout=900)
    info: dict = {}
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and '"reason_code"' in line:
            try:
                info = json.loads(line)
            except Exception:
                info = {}
            break
    reason = info.get("reason_code") or ("ok" if code == 0 else "rejected")
    status = REPOUR_STATUS.get(reason, "rejected")
    ok = status in ("auto", "not_needed")
    res = StepResult(ok=ok, status=status, auto=True, reason=reason,
                     pours=info.get("pours"), poured=info.get("poured_after"),
                     fills=info.get("fills"), ms=info.get("ms"),
                     opened_pcb=info.get("opened_pcb"),
                     log=(out or "").strip())
    if not ok:
        res["next"] = REPOUR_NEXT.get(status, REPOUR_NEXT["rejected"])
        res["log"] = (res["log"] + "\n" + res["next"]).strip()
    return res


def step_drc(cfg: dict) -> StepResult:
    """DRC 三态：执行是否成功（ok）/ 是否通过（drc_passed）/ 是否可发布（publishable）。

    ok=True 只代表"检查跑起来了"，不代表通过；publishable 目前恒为 False ——
    本流水线没有完整的制造放行判定（覆铜是否真重建、规则是否等价、开窗几何都未校验）。
    """
    code, log = _run([_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "eda_rules.py"),
                      "--exe", cfg["eda_exe"], "drc"], timeout=1800)
    executed = code == 0
    passed = executed and "passed=True" in log.replace(" ", "")
    return StepResult(ok=executed, status="passed" if passed else ("failed" if executed else "error"),
                      log=log, drc_passed=passed, publishable=False)


def step_export(cfg: dict, work: Path) -> StepResult:
    """从"当前已打开的工程"导出 Gerber 包（用 EDA CLI invoke）。"""
    js = Path(cfg["repo_root"]) / "rebuild_in_jlc" / "js" / "export_current_gerber.js"
    code, log = _run([cfg["eda_exe"], "invoke", "--session", _current_session(cfg),
                      "--code-file", str(js), "--timeout", "300000"], timeout=1800)
    out_zip = work / "gerber" / "Gerber.zip"
    payload = None
    for line in log.splitlines():
        line = line.strip()
        if line.startswith("{") and '"b64"' in line:
            payload = line
    if not payload:
        return StepResult(ok=False, log=log)
    import base64
    data = json.loads(payload).get("value") or {}
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(base64.b64decode(data["b64"]))
    return StepResult(ok=True, log=log, zip=str(out_zip), size=data.get("size"))


def _current_session(cfg: dict) -> str:
    code, out = _run([cfg["eda_exe"], "session", "list"], timeout=120)
    try:
        sessions = json.loads(out[out.index("{"):]).get("value") or []
    except Exception:
        sessions = []
    for s in sessions:
        if s.get("origin") == "user" and s.get("status") == "alive":
            return s["sessionId"]
    raise RuntimeError("没有在线的用户会话：请先在嘉立创EDA 里登录并打开任意工程")


def step_compare(cfg: dict, eda_zip: str, board: Path, work: Path) -> StepResult:
    ref = work / "kicad_ref"
    ref.mkdir(parents=True, exist_ok=True)
    cli = _find_kicad_cli()
    if not cli:
        return StepResult(ok=False, log="找不到 kicad-cli，跳过对照（用 JLC_KICAD_CLI 指定）")
    _run([cli, "pcb", "export", "gerbers", "--output", str(ref) + os.sep, str(board)], timeout=1800)
    _run([cli, "pcb", "export", "drill", "--format", "excellon",
          "--excellon-separate-th", "--output", str(ref) + os.sep, str(board)], timeout=1800)
    # 源板铜层数：决定内层是"不适用"还是"缺证据"。用 KiCad python 读，失败则留空
    copper = _source_copper(cfg, board)
    cmd = [_py_for_eda(cfg), str(Path(cfg["repo_root"]) / "tools" / "compare_gerber_layers.py"),
           eda_zip, str(ref), "--json", str(work / "layer_compare.json")]
    if copper:
        cmd += ["--source-copper", str(copper)]
    code, log = _run(cmd, timeout=900)
    (work / "layer_compare.md").write_text(log, encoding="utf-8")
    # 对照是"取证"步骤：只要工具跑完就算执行成功，差异/缺证据用 status 表达。
    # 这样不会因为"发现了差异"就把整条流程判成失败——差异本身要人看。
    res = StepResult(ok=code in (0, 3, 4), log=log,
                     report=str(work / "layer_compare.md"))
    if code == 0:
        res["status"] = "compared"
    elif code == 3:
        res["status"] = "missing_evidence"
        res["next"] = "对照缺证据（缺层/缺钻孔/解析失败），详见 layer_compare.md"
    elif code == 4:
        res["status"] = "differs"
        res["next"] = "对照发现整体偏移，详见 layer_compare.md"
    else:
        res["status"] = "failed"
        res["ok"] = False
        res["next"] = f"对照工具执行失败（退出码 {code}）"
    return res


def _source_copper(cfg: dict, board: Path) -> int | None:
    """读源板铜层数（用于对照工具判断内层是否“不适用”）。"""
    code, out = _run([_python_for_board(cfg), "-c",
                      "import sys, pcbnew; b=pcbnew.LoadBoard(sys.argv[1]); "
                      "print(int(b.GetCopperLayerCount()))", str(board)], timeout=120)
    if code == 0:
        for token in (out or "").split():
            if token.isdigit():
                return int(token)
    return None


def _find_kicad_cli() -> str | None:
    env = os.environ.get("JLC_KICAD_CLI")
    if env and Path(env).exists():
        return env
    import shutil
    hit = shutil.which("kicad-cli")
    if hit:
        return hit
    for p in (r"D:\Software\Electrical\KiCAD10\bin\kicad-cli.exe",
              r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"):
        if Path(p).exists():
            return p
    return None


# --------------------------------------------------------------------------- 编排
def _run_impl(board: Path, name: str, work: Path, stop_after: str | None = None,
        repour_confirmed: bool = False, on_step=None,
        export_when_drc_failed: bool = False, auto_repour: bool = True,
        continue_on_error: bool = False) -> list[StepResult]:
    cfg = config.load()
    problems = config.check(cfg)
    if problems:
        raise RuntimeError("；".join(problems))
    work.mkdir(parents=True, exist_ok=True)

    results: list[StepResult] = []
    py_eda = _py_for_eda(cfg)
    py_board = _python_for_board(cfg)

    state_path = work / "run.json"

    def save_state(finished: bool = False, note: str = "") -> None:
        state = {
            "board": str(board),
            "name": name,
            "work_dir": str(work),
            "finished": finished,
            "note": note,
            "steps": [{k: r.get(k) for k in ("step", "title", "ok", "status",
                                             "drc_passed", "publishable", "uuid")}
                      for r in results],
        }
        try:
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def emit(key: str, res: StepResult) -> StepResult:
        res["step"] = key
        res["title"] = STEP_TITLES[key]
        res.setdefault("status", "done" if res.get("ok") else "failed")
        results.append(res)
        (work / f"step_{key}.log").write_text(res.get("log") or "", encoding="utf-8")
        console.step(res["title"], "OK" if res.get("ok") else f"FAILED({res.get('status')})")
        if res.get("uuid"):
            console.info(f"  工程 UUID：{res['uuid']}")
        if res.get("drc_passed") is not None:
            console.info(f"  DRC 通过：{res['drc_passed']}")
        if res.get("zip"):
            console.info(f"  Gerber：{res['zip']}")
        save_state()
        if on_step:
            on_step(key, res)
        return res

    (work / "env.log").write_text(
        f"python_for_board = {py_board}\npython_for_eda = {py_eda}\n"
        f"eda_exe = {cfg.get('eda_exe')}\n", encoding="utf-8")
    r = emit("convert", step_convert(cfg, board, work, name))
    if not r.ok:
        return results
    project = r["project"]
    if stop_after == "convert":
        return results

    r = emit("to_epro2", step_to_epro2(cfg, project, work))
    if not r.ok:
        return results
    epro2 = r["epro2"]
    if stop_after == "to_epro2":
        return results

    proj_name = f"KiCad_{name}_{time.strftime('%Y%m%d_%H%M%S')}"
    console.info(f"  工程名：{proj_name}")
    r = emit("import", step_import(cfg, epro2, work, proj_name))
    if not r.ok:
        return results
    uuid = r["uuid"]
    if stop_after == "import":
        return results

    r = emit("open", step_open(cfg, uuid))
    if not r.ok:
        save_state(note="打开目标工程失败（或打开的不是导入的那个工程），已停止")
        return results
    if stop_after == "open":
        save_state(note="按 --stop-after open 停止")
        return results

    rk = emit("knockout", step_knockout(cfg, work, name))
    if not rk.ok and not continue_on_error:
        save_state(finished=True, note="反相补写失败，已阻断（诊断可用 --continue-on-error）")
        return results
    if stop_after == "knockout":
        save_state(note="按 --stop-after knockout 停止")
        return results

    rr = emit("rules", step_rules(cfg))
    if not rr.ok and not continue_on_error:
        save_state(finished=True, note="规则对齐失败，已阻断（诊断可用 --continue-on-error）")
        return results
    if stop_after == "rules":
        save_state(note="按 --stop-after rules 停止")
        return results

    r = emit("repour", step_repour(cfg, auto=auto_repour, manual_confirmed=repour_confirmed))
    if not r.ok:
        save_state(note="停在人工步骤：重建覆铜未确认")
        return results
    if stop_after == "repour":
        save_state(note="按 --stop-after 停止")
        return results

    r = emit("drc", step_drc(cfg))
    if stop_after == "drc":
        save_state(note="按 --stop-after 停止")
        return results
    if not r.get("drc_passed") and not export_when_drc_failed:
        save_state(note="DRC 未通过，已停止（如需强行导出用 --export-anyway）")
        return results

    r = emit("export", step_export(cfg, work))
    if not r.ok:
        return results
    if stop_after == "export":
        save_state(note="按 --stop-after 停止")
        return results

    emit("compare", step_compare(cfg, r["zip"], board, work))
    save_state(finished=True)
    return results


def run(board: Path, name: str, work: Path, stop_after: str | None = None,
        repour_confirmed: bool = False, on_step=None,
        export_when_drc_failed: bool = False, auto_repour: bool = True,
        continue_on_error: bool = False) -> list[StepResult]:
    """跑流水线，并在结束时打印明确的结束/成功信息。"""
    t0 = time.time()
    results: list[StepResult] = []
    try:
        results = _run_impl(board, name, work, stop_after, repour_confirmed, on_step,
                            export_when_drc_failed, auto_repour, continue_on_error)
        return results
    finally:
        _report_end(results, work, time.time() - t0)


def _report_end(results: list[StepResult], work: Path, elapsed: float) -> None:
    """统一的收尾日志：成功/中止、耗时、产物位置。"""
    total = len(results)
    ok_n = sum(1 for r in results if r.get("ok"))
    drc = next((r for r in results if r.get("step") == "drc"), None)
    export = next((r for r in results if r.get("step") == "export" and r.get("ok")), None)
    compare = next((r for r in results if r.get("step") == "compare"), None)
    finished = bool(results) and results[-1].get("step") == "compare" and results[-1].get("ok")

    all_ok = bool(results) and all(r.get("ok") for r in results)
    last = results[-1] if results else {}
    drc_item = next((r for r in results if r.get("step") == "drc"), None)
    drc_failed = drc_item is not None and not drc_item.get("drc_passed")
    if finished:
        console.info("=" * 56)
        console.info(f"✔ 全部完成：{ok_n}/{total} 步执行完毕，总耗时 {elapsed:.1f}s")
    elif all_ok and drc_failed:
        console.info("=" * 56)
        console.info(f"■ DRC 未通过，流程停在此处：{ok_n}/{total} 步成功，总耗时 {elapsed:.1f}s")
        console.info("   如需先拿到 Gerber 做差异分析，可加 --export-anyway 再跑一次。")
    elif all_ok:
        console.info("=" * 56)
        console.info(f"✔ 已按停止点结束：{ok_n}/{total} 步成功，总耗时 {elapsed:.1f}s"
                     f"（停在 {last.get('title', last.get('step'))}）")
        console.info("   这是调试停止点，整个流程并未走完。")
    else:
        console.info("=" * 56)
        console.info(f"✘ 流程中止：{ok_n}/{total} 步成功，总耗时 {elapsed:.1f}s")
        console.info(f"   停在：{last.get('title', last.get('step'))}（{last.get('status', '-')}）")
        if last.get("next"):
            console.info(f"   说明：{last['next']}")
    if drc is not None:
        console.info(f"   DRC：{'通过' if drc.get('drc_passed') else '未通过（明细见 step_drc.log）'}")
    console.info(f"   工作目录：{work}")
    if export:
        console.info(f"   Gerber ：{export.get('zip')}")
    if compare and compare.get("report"):
        console.info(f"   对照报告：{compare['report']}")
        _cs = compare.get("status")
        if _cs == "differs":
            console.info("   对照结论：发现整体偏移（不等价，见报告）")
        elif _cs == "missing_evidence":
            console.info("   对照结论：缺证据（缺层/缺钻孔，不能视为等价）")
    console.info("=" * 56)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KiCad → 嘉立创EDA 重建流水线（插件与 CLI 共用）")
    ap.add_argument("--board", required=True, help=".kicad_pcb 路径")
    ap.add_argument("--name", default=None, help="工程名（默认取板文件名）")
    ap.add_argument("--out", default=None, help="工作目录（默认 <板目录>/out/rebuild_in_jlc_<时间戳>）")
    ap.add_argument("--stop-after", default=None, choices=list(STEP_TITLES),
                    help="跑到指定步骤为止（调试用）")
    ap.add_argument("--repour-confirmed", action="store_true",
                    help="声明已在 EDA 里手工执行「设计 → 重建覆铜」；不传则停在该步骤")
    ap.add_argument("--no-auto-repour", action="store_true",
                    help="不尝试自动重铺，直接要求人工确认覆铜")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="必需步骤失败时仍继续（仅用于诊断，会在 run.json 记录原因）")
    ap.add_argument("--export-anyway", action="store_true",
                    help="DRC 未通过时仍然导出 Gerber（仅用于取差异，不代表通过）")
    ap.add_argument("--check-config", action="store_true", help="只检查配置")
    a = ap.parse_args(argv)

    cfg = config.load()
    problems = config.check(cfg)
    if a.check_config:
        print("配置：", json.dumps(cfg, ensure_ascii=False, indent=2))
        print("问题：", problems or "无")
        return 0 if not problems else 1
    if problems:
        print("配置有问题：", "；".join(problems))
        return 1

    board = Path(a.board).expanduser().resolve()
    if not board.exists():
        print("板文件不存在：", board)
        return 1
    name = a.name or board.stem
    work = Path(a.out) if a.out else board.parent / "out" / time.strftime("rebuild_in_jlc_%Y%m%d_%H%M%S")

    def show(key, res):
        print(f"\n=== {res['title']} → {'OK' if res['ok'] else 'FAILED'}")

    results = run(board, name, work, a.stop_after, a.repour_confirmed, show,
                  a.export_anyway, auto_repour=not a.no_auto_repour,
                  continue_on_error=a.continue_on_error)
    print(f"\n工作目录：{work}")
    if not results:
        print("没有任何步骤执行")
        return 1

    failed = [r for r in results if not r["ok"]]
    drc = next((r for r in results if r.get("step") == "drc"), None)
    done_all = results[-1].get("step") == "compare" and results[-1].get("ok")

    print("\n状态汇总：")
    for r in results:
        extra = ""
        if r.get("step") == "drc":
            extra = f"（DRC {'通过' if r.get('drc_passed') else '未通过'}）"
        print(f"  {r['title']:<16}{'完成' if r['ok'] else '未完成'}{extra}")
    if failed:
        print("未完成/失败步骤：", ", ".join(r["step"] for r in failed))
    if drc and not drc.get("drc_passed"):
        print("注意：DRC 未通过 —— 这一步只说明检查跑起来了，不代表设计可用。")
    if not done_all:
        print("流程停在中间状态；这不是\"可用于投板\"的结论。")
        return 1
    print("全流程执行完毕（仍不等于可投板：覆铜/规则/开窗均需人工复核）。")
    console.info(f"✔ 成功：{len(results)} 步执行完毕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
