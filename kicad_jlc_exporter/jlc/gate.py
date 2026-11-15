"""发布判定（release gate）：把所有阶段的结论合并成一个"能不能投板"的决定。

设计原则（对应评审 S-03 / S-06 / S-07 / S-09 / S-10）：
  * **没有一个阶段被跳过，就等于没通过**——覆盖率是判定的一部分；
  * 任何阶段出现 ERROR 都阻断发布，而不是只看文件级预检；
  * 解析器遇到不支持的语法时，"未检查"本身即阻断；
  * 阻断时只产出诊断包（`*_DIAGNOSTIC.zip`），绝不产出与正式包同名的文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

StageStatus = Literal["passed", "failed", "skipped"]


@dataclass
class Stage:
    name: str
    status: StageStatus
    detail: str = ""
    required: bool = True


@dataclass
class GateDecision:
    stages: list[Stage] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str, status: StageStatus, detail: str = "", required: bool = True):
        self.stages.append(Stage(name, status, detail, required))
        if status == "failed":
            self.blockers.append(f"{name}: {detail}" if detail else f"{name} 未通过")
        elif status == "skipped" and required:
            self.blockers.append(f"{name}: 未执行（必需阶段）")
        elif status == "skipped":
            self.warnings.append(f"{name}: 未执行（可选阶段）")

    # -------------------------------------------------- 判定
    @property
    def release(self) -> bool:
        """无硬阻断（ERROR/未实现语法/冲突）。"""
        return not self.blockers

    @property
    def coverage_complete(self) -> bool:
        """所有必需阶段都真的跑了。"""
        return all(s.status != "skipped" for s in self.stages if s.required)

    @property
    def publishable(self) -> bool:
        """可以产出正式投板包：无硬阻断 **且** 覆盖完整。"""
        return self.release and self.coverage_complete

    @property
    def coverage(self) -> dict[str, str]:
        return {s.name: s.status for s in self.stages}

    def to_text(self) -> str:
        verdict = ("允许发布" if self.publishable else
                   ("禁止发布：有硬阻断" if not self.release else "条件允许：有必需阶段未执行"))
        out = ["—" * 72, "发布判定（release gate）", f"结论：{verdict}"]
        for s in self.stages:
            flag = {"passed": "OK  ", "failed": "FAIL", "skipped": "SKIP"}[s.status]
            opt = "" if s.required else "（可选）"
            out.append(f"  [{flag}] {s.name}{opt}" + (f" —— {s.detail}" if s.detail else ""))
        for b in self.blockers:
            out.append(f"  · 阻断：{b}")
        for w in self.warnings:
            out.append(f"  · 提示：{w}")
        out.append("—" * 72)
        return "\n".join(out)

    def to_manifest(self) -> dict:
        return {
            "release": self.release,
            "publishable": self.publishable,
            "coverage_complete": self.coverage_complete,
            "coverage": self.coverage,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "stages": [{"name": s.name, "status": s.status, "detail": s.detail,
                        "required": s.required} for s in self.stages],
        }


def apply_drc_waivers(by_type: dict[str, int],
                      waivers: dict[str, int] | None
                      ) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """核算 DRC 豁免。

    规则：豁免必须写明"规则类型 + 精确条数"，少一条或多一条都不放行。
    返回 (剩余违规按类, 生效豁免, 错误说明)。纯函数，便于离线回归。
    """
    remaining = dict(by_type)
    applied: dict[str, int] = {}
    problems: list[str] = []
    for kind, cnt in (waivers or {}).items():
        have = remaining.get(kind, 0)
        if have == cnt and cnt > 0:
            applied[kind] = cnt
            remaining.pop(kind, None)
        else:
            problems.append(
                f"{kind} 声明豁免 {cnt} 条，实际 {have} 条："
                f"豁免必须与实测条数完全一致（不接受“大致相同”）")
    return remaining, applied, problems
