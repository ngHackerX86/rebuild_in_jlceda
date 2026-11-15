# -*- coding: utf-8 -*-
"""发布清单：BUILD_INFO.json 与包内容一致、不含 docx、只含 git 跟踪文件。

需要先构建过一次（`python scripts/build_release.py --version 0.3.0`）；
没有发布包时本用例会跳过（返回 0 并提示），避免 CI 里因缺产物而红。

运行：python tests/offline/test_release_manifest.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "dist"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    dirs = sorted(p for p in DIST.glob("rebuild_in_jlc-*") if p.is_dir())
    if not dirs:
        print("  [SKIP] dist/ 下没有已构建的发布包（先跑 scripts/build_release.py）")
        return 0
    stage = dirs[-1]
    info_path = stage / "BUILD_INFO.json"
    if not info_path.exists():
        print(f"  [FAIL] {stage.name} 缺 BUILD_INFO.json")
        return 1

    info = json.loads(info_path.read_text(encoding="utf-8"))
    failed = 0

    print(f"  构建信息：version={info['version']} commit={info['source_commit_short']} "
          f"dirty={info['git_dirty']} files={info['file_count']}")

    listed = {f["path"]: f for f in info["files"]}
    on_disk = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    extra = sorted(on_disk - set(listed) - {"BUILD_INFO.json", "VERSION.json", "install.ps1"})
    ok = not extra
    print(f"  [{'PASS' if ok else 'FAIL'}] 包内没有清单之外的文件"
          + (f"（多出：{extra[:5]}）" if extra else ""))
    failed += 0 if ok else 1

    bad = []
    for rel, meta in listed.items():
        p = stage / rel
        if not p.exists() or sha256_of(p) != meta["sha256"] or p.stat().st_size != meta["size"]:
            bad.append(rel)
    ok = not bad
    print(f"  [{'PASS' if ok else 'FAIL'}] 清单里的 SHA256/大小与文件一致"
          + (f"（不符：{bad[:5]}）" if bad else ""))
    failed += 0 if ok else 1

    docx = [p for p in on_disk if p.lower().endswith((".docx", ".doc", ".pdf"))]
    ok = not docx
    print(f"  [{'PASS' if ok else 'FAIL'}] 不含 docx/doc/pdf" + (f"（有：{docx}）" if docx else ""))
    failed += 0 if ok else 1

    tmpl = [p for p in on_disk if p.endswith(".epcb2")]
    ok = any("PCB1_min" in p for p in tmpl) and not any("PCB1.epcb2" in p for p in tmpl)
    print(f"  [{'PASS' if ok else 'FAIL'}] 只带最小模板：{tmpl}")
    failed += 0 if ok else 1

    ok = (stage / "install.ps1").exists()
    print(f"  [{'PASS' if ok else 'FAIL'}] 含 install.ps1")
    failed += 0 if ok else 1

    print("全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


def test_release_manifest():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
