"""F5: validate the draft with the IR toolchain and regenerate only failing sections."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.falcon.assemble import (
    Assembled,
    AssemblyProblem,
    add_references,
    assemble,
    dump_spec,
)
from afsim_research_agent.falcon.autofix import autofix
from afsim_research_agent.falcon.enforce import enforce
from afsim_research_agent.falcon.fill import DraftState, fill_section, refresh_generated
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import SECTIONS, EnforcementNote, RepairRound
from afsim_research_agent.ir.diagnostics import Diagnostic
from afsim_research_agent.ir.l2 import run_tests
from afsim_research_agent.ir.loader import load_spec
from afsim_research_agent.ir.normalize import normalize
from afsim_research_agent.ir.validate import (
    ValidationResult,
    load_evidence_index,
    validate_data,
    validate_package,
)

_TRANSITION = re.compile(r"^roles\[\d+\]\.state_machine\.states\[\d+\]\.transitions")


def section_for(path: str) -> str | None:
    if path.startswith(("parameters", "assumptions")):
        return "parameters"
    if path.startswith(("formations", "coordination", "messages")):
        return "structure"
    if path.startswith("conditions"):
        return "conditions"
    if path.startswith("behaviors"):
        return "behaviors"
    if path.startswith("trees") or _TRANSITION.match(path):
        return "control"
    if path.startswith("roles") and (".blackboards" in path or ".dependencies" in path):
        return "behaviors"
    if path.startswith("roles"):
        return "control"
    if path.startswith(("tests", "scenarios")):
        return "verification"
    return None


@dataclass
class Problem:
    section: str | None
    code: str
    path: str
    message: str
    suggestion: str | None = None

    def render(self) -> str:
        hint = f"（建议：{self.suggestion}）" if self.suggestion else ""
        return f"- {self.code} {self.path}: {self.message}{hint}"


@dataclass
class CheckResult:
    diagnostics: list[Diagnostic]
    l2: dict[str, Any] | None
    l3: list[dict[str, Any]] | None
    problems: list[Problem] = field(default_factory=list)
    ir: dict[str, Any] | None = None  # draft-normalized IR, for the capability check
    # False when behavior_spec.yaml could not even be loaded: validation stopped there,
    # so the problem count says nothing about the rest of the draft
    loaded: bool = True

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def score(self) -> tuple[int, int]:
        """Lower is better; an unloadable draft is worse than any loadable one."""
        return (0 if self.loaded else 1, len(self.problems))


def check_package(
    package_dir: Path, assembly: list[AssemblyProblem], *, with_l3: bool = False
) -> CheckResult:
    """L1 + draft normalization + L2 (+ optional L3 for reference)."""
    validation = validate_package(package_dir)
    norm = normalize(validation, draft=True)
    diagnostics = list(norm.diagnostics)
    problems = [
        Problem(section_for(d.path), d.code, d.path, d.message, d.suggestion)
        for d in diagnostics
        if d.severity.value == "error"
    ]
    problems += [Problem(a.section, "FALASM", a.path, a.message) for a in assembly]
    l2 = l3 = None
    test_ir = norm.ir if norm.ir is not None else _without_scenarios(validation, package_dir)
    if test_ir is not None:
        report = run_tests(test_ir)
        l2 = report.to_dict()
        diagnostics += report.diagnostics
        for d in report.diagnostics:
            if d.severity.value == "error":
                problems.append(
                    Problem(section_for(d.path), d.code, d.path, d.message, d.suggestion)
                )
        for outcome in report.outcomes:
            if not outcome.passed:
                problems.append(
                    Problem(
                        "verification",
                        "TEST001",
                        f"tests.{outcome.test_id}",
                        "; ".join(outcome.failures)
                        + f"；实际结果 next_state={outcome.result.get('next_state')}, "
                        f"fired_transition={outcome.result.get('fired_transition')}, "
                        f"selected_path={outcome.result.get('selected_path')}",
                    )
                )
        from afsim_research_agent.falcon.sheet.probes import oscillation_probes, reaction_probes

        for path, message in reaction_probes(test_ir):
            problems.append(Problem("control", "PROBE001", path, message))
        for path, message in oscillation_probes(test_ir):
            problems.append(Problem("control", "PROBE002", path, message))
        from afsim_research_agent.falcon.sheet.properties import check_properties, load_properties

        properties = load_properties(package_dir)
        if properties:
            failed, _notes = check_properties(test_ir, properties)
            for path, message in failed:
                problems.append(Problem("control", "PROP001", path, message))
        if with_l3 and not problems and norm.ir is not None and norm.ir.get("scenarios"):
            l3 = _run_l3(norm.ir, package_dir)
    return CheckResult(diagnostics, l2, l3, problems, norm.ir, validation.spec is not None)


def _without_scenarios(validation: ValidationResult, package_dir: Path) -> dict[str, Any] | None:
    """When only the L3 scenarios fail L1, normalize the rest so the L2 tests still run.

    The L1 gate is all-or-nothing; without this, one bad scenario hides every L2 test
    result and the repair loop cannot see (or fix) them in the same round."""
    errors = [d for d in validation.diagnostics if d.severity.value == "error"]
    if validation.spec is None or not errors:
        return None
    if not all(d.path.startswith("scenarios") for d in errors):
        return None
    loaded = load_spec(package_dir / "behavior_spec.yaml")
    if loaded.data is None:
        return None
    data = dict(loaded.data)
    data["scenarios"] = []
    result = validate_data(
        data, evidence=load_evidence_index(package_dir / "evidence_package.json")
    )
    kept = [d for d in result.diagnostics if not d.path.startswith("scenarios")]
    stripped = ValidationResult(result.spec, result.effective, result.expansion, kept)
    return normalize(stripped, draft=True).ir


def _run_l3(ir: dict[str, Any], package_dir: Path) -> list[dict[str, Any]] | None:
    from afsim_research_agent.ir.binding import bind, load_overrides
    from afsim_research_agent.ir.sim import run_scenarios

    context, _ = bind(ir, "python_sim", load_overrides(package_dir, "python_sim"))
    if context is None:
        return None
    return [r.to_dict() for r in run_scenarios(ir, context, package_dir=package_dir)]


@dataclass
class DraftOutcome:
    assembled: Assembled
    check: CheckResult
    enforcement: list[EnforcementNote]
    rounds: list[RepairRound]


def write_draft(state: DraftState, package_dir: Path) -> tuple[Assembled, list[EnforcementNote]]:
    assembled = assemble(state.request, state.skeleton, state.sections, state.package)
    fixes = autofix(assembled.data, state.catalog)
    notes = fixes + enforce(
        assembled.data, state.package, state.user_values, state.clauses, assembled.implements
    )
    add_references(assembled.data, state.package)
    (package_dir / "behavior_spec.yaml").write_text(dump_spec(assembled.data), encoding="utf-8")
    return assembled, notes


def feedback_for(problems: list[Problem]) -> str:
    lines = "\n".join(p.render() for p in problems[:40])
    more = f"\n（另有 {len(problems) - 40} 条未列出）" if len(problems) > 40 else ""
    return (
        "\n上一次的完整草稿经 IR 校验发现本段有以下错误。请修正后完整重新输出本段"
        "（保持其他段已使用的 ID 不变）：\n" + lines + more
    )


async def draft_with_repair(
    state: DraftState,
    package_dir: Path,
    run: ModelRunner,
    *,
    max_rounds: int = 3,
    with_l3: bool = False,
) -> DraftOutcome:
    assembled, notes = write_draft(state, package_dir)
    check = check_package(package_dir, assembled.problems, with_l3=with_l3)
    rounds: list[RepairRound] = []
    best = (check.score, 0, dict(state.sections))
    for round_no in range(1, max_rounds + 1):
        if check.ok:
            break
        by_section: dict[str, list[Problem]] = {}
        for problem in check.problems:
            if problem.section is not None:
                by_section.setdefault(problem.section, []).append(problem)
        rounds.append(
            RepairRound(
                round=round_no,
                errors=len(check.problems),
                sections=[s for s in SECTIONS if s in by_section],
                codes=sorted({p.code for p in check.problems}),
                loaded=check.loaded,
            )
        )
        if not by_section:
            break  # nothing a section can fix
        last_round = round_no == max_rounds
        if len(by_section) > 1 and "verification" in by_section and not last_round:
            # tests often fail only because the model they test is broken; fix the
            # model first, but give the tests their turn in the final round
            del by_section["verification"]
        rounds[-1].sections = [s for s in SECTIONS if s in by_section]
        for section in SECTIONS:
            if section in by_section:
                await fill_section(state, section, run, feedback_for(by_section[section]))
        assembled, notes = write_draft(state, package_dir)
        check = check_package(package_dir, assembled.problems, with_l3=with_l3)
        if check.score < best[0]:
            best = (check.score, round_no, dict(state.sections))
    if check.score > best[0]:
        # a later round made things worse: keep the best draft seen
        errors_now = (
            len(check.problems) if check.loaded else f"{len(check.problems)}（草稿无法加载）"
        )
        state.sections = dict(best[2])
        refresh_generated(state)
        assembled, notes = write_draft(state, package_dir)
        check = check_package(package_dir, assembled.problems, with_l3=with_l3)
        label = "首次生成" if best[1] == 0 else f"第 {best[1]} 轮修复后"
        notes.append(
            EnforcementNote(
                path="$",
                rule="repair_rollback",
                detail=f"最后一轮后有 {errors_now} 个错误，已回退到{label}的草稿（{len(check.problems)} 个错误）",
            )
        )
    return DraftOutcome(assembled, check, notes, rounds)
