# -*- coding: utf-8 -*-
"""打包 KiCad 插件发布包（zip），并生成可核验的构建清单。

原则（对应复审建议 §2 / §10）：
  * **只打包 Git 跟踪的文件**——本地多出来的 docx、草稿、构建产物一律不进包；
  * 记录源 commit、是否 dirty、每个文件的 SHA256 与大小（`BUILD_INFO.json`）；
  * 干净 checkout 下用同一命令可复现同样的清单。

产物结构（解压后可直接运行 install.ps1）：

    rebuild_in_jlc-<版本>/
      rebuild_in_jlc/          KiCad 插件（重建到嘉立创EDA）
      kicad_jlc_exporter/      KiCad 插件（Gerber 检查/打包）
      tools/                   流水线运行时脚本
      docs/*.md                文档（不含 docx）
      install.ps1              一键安装
      README.md  LICENSE  THIRD_PARTY_NOTICES.md
      VERSION.json  BUILD_INFO.json

用法：
    python scripts/build_release.py [--version 0.3.0] [--out dist]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PACKAGES = ["rebuild_in_jlc", "kicad_jlc_exporter"]
TOOLS_DIR = "tools"
DOC_FILES = ["README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"]
DOC_DIRS = ["docs"]
WITH_DIRS = ["tests"]        # 离线回归用例，随包分发便于复核
IGNORE_SUFFIX = (".docx", ".doc", ".pdf", ".pyc", ".log")

INSTALL_PS1 = r"""# 一键安装：把两个插件包复制到 KiCad 的用户插件目录，并写入配置
# 用法（PowerShell 5.1 / 7 均可）：
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   .\install.ps1 -PluginsDir "<KiCad 的插件目录>"
param(
    [string]$PluginsDir = "",
    [string]$RepoRoot   = ""
)
$ErrorActionPreference = 'Stop'

if (-not $RepoRoot -or $RepoRoot -eq '') { $RepoRoot = $PSScriptRoot }
if (-not $RepoRoot -or $RepoRoot -eq '') { $RepoRoot = (Get-Location).Path }

# 插件目录：优先用参数；否则按 KiCad 配置目录推断
if (-not $PluginsDir -or $PluginsDir -eq '') {
    $candidates = @()
    if ($env:APPDATA) { $candidates += (Join-Path $env:APPDATA 'kicad\10.0\scripting\plugins') }
    if ($env:USERPROFILE) { $candidates += (Join-Path $env:USERPROFILE 'Documents\KiCad\10.0\scripting\plugins') }
    foreach ($c in $candidates) {
        if (Test-Path (Split-Path $c -Parent)) { $PluginsDir = $c; break }
    }
    if (-not $PluginsDir -or $PluginsDir -eq '') { $PluginsDir = $candidates[0] }
}
Write-Host "插件目录：$PluginsDir"
if (-not (Test-Path $PluginsDir)) {
    New-Item -ItemType Directory -Force -Path $PluginsDir | Out-Null
}
Write-Host '提示：以 KiCad 里「工具 -> 外部插件 -> 打开插件目录」显示的路径为准；若不同请用 -PluginsDir 指定。'

foreach ($pkg in @('rebuild_in_jlc', 'kicad_jlc_exporter')) {
    $src = Join-Path $RepoRoot $pkg
    if (-not (Test-Path $src)) { Write-Warning "找不到包 $src，跳过"; continue }
    $dst = Join-Path $PluginsDir $pkg
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item $src $dst -Recurse
    Write-Host "已安装 $pkg -> $dst"
}

# 写配置（兼容 PowerShell 5.1：不用 ConvertFrom-Json -AsHashtable）
$cfgDir = Join-Path $env:APPDATA 'rebuild_in_jlc'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
$cfgPath = Join-Path $cfgDir 'config.json'
$cfg = @{}
if (Test-Path $cfgPath) {
    try {
        $raw = Get-Content $cfgPath -Raw -Encoding UTF8
        $obj = $raw | ConvertFrom-Json
        foreach ($p in $obj.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    } catch {
        Write-Warning "读取已有配置失败，本次不覆盖它：$cfgPath（$($_.Exception.Message)）"
        $cfg = $null
    }
}
if ($null -ne $cfg) {
    $cfg['repo_root'] = $RepoRoot
    if (-not $cfg.ContainsKey('eda_exe') -or -not $cfg['eda_exe']) {
        $guess = 'D:\Software\Electrical\lceda-proV4\lceda-pro.exe'
        if (Test-Path $guess) { $cfg['eda_exe'] = $guess }
    }
    if (-not $cfg.ContainsKey('kicad_python') -or -not $cfg['kicad_python']) {
        $kpy = Get-ChildItem 'D:\Software\Electrical' -Directory -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -like 'KiCAD*' } |
               ForEach-Object { Join-Path $_.FullName 'bin\python.exe' } |
               Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($kpy) { $cfg['kicad_python'] = $kpy }
    }
    $json = $cfg | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "已写配置 $cfgPath（repo_root=$RepoRoot）"
    if (-not (Test-Path (Join-Path $RepoRoot 'tools\kicad2eprj3.py'))) {
        Write-Warning "在 $RepoRoot 下找不到 tools/kicad2eprj3.py：请把 repo_root 指向含 tools/ 的目录"
    }
}

Write-Host ""
Write-Host '下一步：打开 KiCad PCB 编辑器 -> 工具 -> 外部插件 -> 刷新插件'
Write-Host '菜单里应出现「重建到嘉立创EDA（KiCad->JLCEDA）」与「导出嘉立创 Gerber 包…」'
"""


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()
    except Exception:
        return ""


def require_git() -> None:
    """没有 Git 元数据时不允许生成"空成功包"（对应复审 N06）。"""
    top = _git("rev-parse", "--show-toplevel")
    if not top:
        raise SystemExit("当前目录不是 Git 仓库：无法确定要打包哪些文件；请在仓库内构建（暂无源码归档模式）")
    if Path(top).resolve() != ROOT.resolve():
        raise SystemExit(f"✘ Git 仓库根不是本项目：{top}")


def tracked_files() -> list[str]:
    """只取 git 跟踪的文件（本地私有文档/产物不会进包）。

    用 `-z`：默认配置下 git 会把非 ASCII 路径（docs/中文.md）加引号转义，
    那样 `startswith("docs/")` 就匹配不上了——之前正是这么漏掉整个 docs 目录的。
    """
    import subprocess as _sp
    try:
        proc = _sp.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True)
        text = proc.stdout.decode("utf-8", "replace")
        files = [f for f in text.split("\0") if f.strip()]
        return sorted(files)
    except Exception:
        return sorted(line.strip() for line in _git("ls-files").splitlines() if line.strip())


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def select(files: list[str]) -> list[str]:
    keep = []
    for f in files:
        if f.endswith(IGNORE_SUFFIX):
            continue
        top = f.split("/")[0]
        if top in PACKAGES or top == TOOLS_DIR:
            keep.append(f)
        elif f in DOC_FILES:
            keep.append(f)
        elif f.startswith("docs/images/"):
            # README 用的截图只在 GitHub 上渲染，不进发布包（避免体积膨胀）
            continue
        elif any(f.startswith(d + "/") for d in DOC_DIRS + WITH_DIRS):
            keep.append(f)
    return keep


def build(out_dir: Path, version: str) -> Path:
    require_git()
    stage = out_dir / f"rebuild_in_jlc-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    files = select(tracked_files())
    manifest = []
    for rel in files:
        src = ROOT / rel
        if not src.is_file():
            continue
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest.append({"path": rel, "size": dst.stat().st_size, "sha256": sha256_of(dst)})

    (stage / "install.ps1").write_text(INSTALL_PS1, encoding="utf-8-sig")
    manifest.append({"path": "install.ps1", "size": (stage / "install.ps1").stat().st_size,
                     "sha256": sha256_of(stage / "install.ps1")})

    dirty = _git("status", "--porcelain")
    meta = {
        "name": "rebuild_in_jlc",
        "version": version,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_commit": _git("rev-parse", "HEAD") or "unknown",
        "source_commit_short": _git("rev-parse", "--short", "HEAD") or "unknown",
        "git_dirty": bool(dirty),
        "packages": [p for p in PACKAGES if (stage / p).is_dir()],
        "file_count": len(manifest) + 2,        # + VERSION.json + BUILD_INFO.json
        "files": manifest,
        "note": "解压后运行 install.ps1；本清单可核对包内容（SHA256）",
    }
    (stage / "BUILD_INFO.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    (stage / "VERSION.json").write_text(json.dumps(
        {"name": "rebuild_in_jlc", "version": version, "packages": meta["packages"],
         "commit": meta["source_commit_short"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    manifest.append({"path": "VERSION.json", "size": (stage / "VERSION.json").stat().st_size,
                     "sha256": sha256_of(stage / "VERSION.json")})
    meta["file_count"] = len(manifest) + 1          # + BUILD_INFO.json 自身
    (stage / "BUILD_INFO.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                           encoding="utf-8")

    zip_path = out_dir / f"rebuild_in_jlc-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out_dir))

    if dirty:
        lines = dirty.splitlines()
        print(f"! 工作树不干净（{len(lines)} 项未提交），仍按当前文件构建：")
        for line in lines[:8]:
            print("    " + line)
        print("    正式发布建议先提交，再从干净 checkout 构建。")
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
    stage_files = sorted(p.relative_to(out_dir).as_posix()
                         for p in stage.rglob("*") if p.is_file())
    if names != stage_files:
        raise SystemExit(f"✘ ZIP 内容与 stage 不一致（zip {len(names)} / stage {len(stage_files)}）")
    if len(names) != meta["file_count"]:
        raise SystemExit(f"✘ 清单声明 {meta['file_count']} 个文件，实际 {len(names)} 个")
    print(f"发布包：{zip_path}（{zip_path.stat().st_size / 1024:.0f} KB，"
          f"{meta['file_count']} 个文件，commit {meta['source_commit_short']}"
          f"{'，dirty' if meta['git_dirty'] else '，clean'}）")
    return zip_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包 KiCad 插件发布包（只含 git 跟踪文件）")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    ap.add_argument("--version", default="0.4.0")
    a = ap.parse_args(argv)
    out = (ROOT / a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    build(out, a.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
