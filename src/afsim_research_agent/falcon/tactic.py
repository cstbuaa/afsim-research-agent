"""P4 / C6: the tactic the user selected in Crow C5 as Falcon input.

``tactic_selection.yaml`` (written by ``crow.select``) names candidates, the revision
the user looked at and, when combining, the phases taken from each. Falcon:

- refuses when a candidate is missing, was revised after the selection, or a phase
  does not exist (before anything is written);
- turns the selected steps, exceptions and formations into clauses (``origin:
  tactic``) that the skeleton must implement, next to the requirement clauses;
- reuses Crow's evidence package so the evidence IDs on the cards resolve, and
  passes them along as the basis of those clauses;
- shows the tactic to every generation stage (``brief``);
- carries the selected candidates' Crow capability gaps into the package
  (``crow_capability_gaps.yaml``), so the IR check — and later ``ir build`` —
  reports which estimated gaps the model really has;
- records the red team's counter-tactics (v1: record only; later L3 scenarios).

The three rule-set card kinds of crow-tactic-proposal-v1 §7 (selection rule / tactic
procedure / coordination) belong to the Falcon v2 generation layer; v1 hands the
selected tactic over as clauses and a brief.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from afsim_research_agent.capability.ir_check import CROW_GAPS_FILE as CROW_GAPS_FILE
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.models import PlatformHint, RequirementClause

SELECTION_FILE = "tactic_selection.yaml"
SELECTED_FILE = "selected_tactics.yaml"
_CLASSES = ("ground_vehicle", "air_vehicle", "surface_vessel", "static_node")
_REFERENCE = re.compile(r"参考值")

TACTIC_RULES = """选定战术（用户在 Crow 评审后确认的建模对象）：
- 模型要实现这个战术，不要重新设计；条款中 origin 为 tactic 的来自战术的步骤、例外和队形，必须被实现，
  并落到具体的转换、行为、条件或协同上（只挂在角色上不算实现）。
- 行为模型无法表达的战术要素（如指挥权接替、无人机或徒步侦察、火力试探）写入 out_of_scope 并说明原因，
  不要挂在无关的元素上。
- 战术条款带的 evidence（ev_ 开头）是该要素的依据，basis.evidence 引用这些 ev_ 编号，**不要把条款编号
  （tc_xx_sN、rq_NN）写进 evidence**；条款编号只写在 implements 中。
- 组合多个候选时，按列出的顺序衔接各阶段；只选了部分阶段的候选，只实现列出的阶段，其余内容仅作参考。
- 战术中出现但资料没有给出的数值（包括标为"参考值"的假设）照常定义为参数，basis.status 写 unknown：
  按参考值构建，审查时标为无依据，由人工复核调参。"""


@dataclass
class SelectedPart:
    card: Any  # TacticCard
    phases: list[str] | None  # None = the whole candidate
    review: Any = None  # RedTeamReview | None

    @property
    def whole(self) -> bool:
        return self.phases is None

    def steps(self) -> list[tuple[int, Any]]:
        return [
            (i, s)
            for i, s in enumerate(self.card.steps)
            if self.phases is None or s.phase in self.phases
        ]


@dataclass
class SelectedTactic:
    directory: Path
    selection_path: Path
    doc: dict[str, Any]
    result: Any  # ProposalResult
    parts: list[SelectedPart]
    profile: Any = None  # MissionProfile | None
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ inputs
    @property
    def candidates(self) -> list[str]:
        return [p.card.id for p in self.parts]

    @property
    def user_notes(self) -> str:
        return str(self.doc.get("user_notes") or "").strip()

    @property
    def source_label(self) -> str:
        return f"{self.directory.name}#{'+'.join(self.candidates)}"

    def requirement(self, stated: str) -> str:
        """The requirement Falcon decomposes: the user's text (or Crow's requirement)
        plus the selection note, so numbers the user wrote there count as stated."""
        text = stated.strip() or str(self.result.requirement).strip()
        if self.user_notes:
            text += f"\n用户选择备注：{self.user_notes}"
        return text

    def platforms(self) -> list[PlatformHint]:
        if self.profile is None:
            return []
        out = []
        for force in self.profile.own_forces:
            if force.platform_class in _CLASSES:
                out.append(
                    PlatformHint(
                        role_hint=force.role_hint,
                        capability_class=force.platform_class,
                        count=min(force.count, 64),
                    )
                )
        return out

    def refine(self) -> list[str]:
        """Capability groups Crow judged finely (kept unless the request says otherwise)."""
        info = self.result.capability_granularity
        path = self.directory / "capability_gaps.yaml"
        if info is None and path.is_file():
            info = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("granularity")
        return [str(g) for g in (info or {}).get("refine") or []]

    def evidence_package(self, base: Path) -> Path | None:
        """Crow's evidence package (its IDs are the ones the cards cite)."""
        recorded = str(self.result.evidence_package or "")
        tries: list[Path] = []
        if recorded:
            path = Path(recorded)
            tries += [path] if path.is_absolute() else [base / path, Path.cwd() / path]
            tries += [self.directory / path.name, self.directory.parent / path.name]
        tries += [
            self.directory / "evidence_package.json",
            self.directory.parent / "evidence_package.json",
        ]
        return next((p for p in tries if p.is_file()), None)

    def research_result(self) -> Path | None:
        for d in (self.directory, self.directory.parent):
            if (d / "research_result.json").is_file():
                return d / "research_result.json"
        return None

    # ------------------------------------------------------------------ clauses
    def clauses(self, evidence_ids: set[str] | None = None) -> list[RequirementClause]:
        """Selected steps, exceptions and formations as clauses (``origin: tactic``).

        ``evidence_ids``: the package's IDs; card evidence outside it is dropped (and
        noted), so a clause never points at evidence the draft cannot cite."""
        out: list[RequirementClause] = []
        dropped = 0

        def add(cid: str, kind: str, text: str, element: Any, roles: list[str]) -> None:
            nonlocal dropped
            evidence = list(element.evidence)
            if evidence_ids is not None:
                kept = [e for e in evidence if e in evidence_ids]
                dropped += len(evidence) - len(kept)
                evidence = kept
            out.append(
                RequirementClause(
                    id=cid,
                    text=text,
                    kind=kind,  # type: ignore[arg-type]
                    roles=roles,
                    origin="tactic",
                    evidence=evidence,
                    tactic_basis=element.basis,
                )
            )

        for part in self.parts:
            card = part.card
            roles = [r.role for r in card.roles]
            for index, step in part.steps():
                text = f"【{step.phase}】{step.do}" + (f"，直到{step.until}" if step.until else "")
                add(f"{card.id}_s{index + 1}", "behavior", text, step, [])
            if not part.whole:
                continue
            for index, item in enumerate(card.geometry):
                text = item.formation + (f"（{item.when}）" if item.when else "")
                text += f"，间距{item.spacing}" if item.spacing else ""
                add(f"{card.id}_g{index + 1}", "formation", text, item, roles)
            for index, item in enumerate(card.exceptions):
                add(
                    f"{card.id}_x{index + 1}",
                    "condition",
                    f"若{item.situation}：{item.then}",
                    item,
                    [],
                )
        if dropped:
            self.notes.append(f"战术卡中 {dropped} 处证据引用不在本次证据包中，已从战术条款中去掉")
        return out

    # ------------------------------------------------------------------ prompts
    def brief(self) -> dict[str, Any]:
        parts = []
        for part in self.parts:
            card = part.card
            entry: dict[str, Any] = {
                "candidate": card.id,
                "name": card.name,
                "revision": card.revision,
                "taken": "全部" if part.whole else f"仅阶段 {'、'.join(part.phases or [])}",
                "summary": card.summary,
                "narrative": card.narrative,
                "decision_authority": card.decision_authority,
                "applicability": [a.situation for a in card.applicability],
                "roles": [{"role": r.role, "members": r.members} for r in card.roles],
                "geometry": [
                    g.model_dump(include={"formation", "when", "spacing"}) for g in card.geometry
                ],
                "steps": [
                    {
                        "clause": f"{card.id}_s{i + 1}",
                        "phase": s.phase,
                        "do": s.do,
                        "until": s.until,
                    }
                    for i, s in part.steps()
                ],
                "exceptions": [
                    e.model_dump(include={"situation", "then"}) for e in card.exceptions
                ],
            }
            if card.assumptions:
                entry["assumptions"] = list(card.assumptions)
            if not part.whole:
                entry["note"] = "只实现所列阶段；队形与例外仅作参考"
            parts.append(entry)
        out: dict[str, Any] = {"parts": parts}
        if len(parts) > 1:
            out["combination"] = "按 parts 的顺序衔接各阶段"
        if self.user_notes:
            out["user_notes"] = self.user_notes
        return out

    def prompt_block(self) -> str:
        import json

        return f"\n{TACTIC_RULES}\n{json.dumps(self.brief(), ensure_ascii=False, separators=(',', ':'))}\n"

    # ------------------------------------------------------------------ records
    def reference_assumptions(self) -> list[tuple[str, str]]:
        return [
            (p.card.id, a) for p in self.parts for a in p.card.assumptions if _REFERENCE.search(a)
        ]

    def counter_tactics(self) -> list[tuple[str, Any]]:
        return [
            (p.card.id, ct)
            for p in self.parts
            if p.review is not None
            for ct in p.review.counter_tactics
        ]

    def crow_gaps(self) -> list[dict[str, Any]] | None:
        path = self.directory / "capability_gaps.yaml"
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        chosen = set(self.candidates)
        return [
            g
            for g in data.get("gaps") or []
            if any(u.get("candidate") in chosen for u in g.get("used_by", []))
        ]

    def crow_gaps_document(self) -> str | None:
        gaps = self.crow_gaps()
        if gaps is None:
            return None
        doc = {
            "format": "CrowCapabilityGaps",
            "source": self.source_label,
            "note": "Crow C4b 对所选战术的能力缺口预估；ir build 用它对照 IR 核对结果",
            "gaps": gaps,
        }
        return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)

    def selected_document(self) -> str:
        doc = {
            "format": "SelectedTactics",
            "selection": self.doc,
            "source": str(self.selection_path),
            "candidates": [
                {
                    "taken": "all" if p.whole else p.phases,
                    "card": p.card.model_dump(
                        mode="json",
                        include={
                            "id",
                            "name",
                            "revision",
                            "provenance",
                            "summary",
                            "narrative",
                            "applicability",
                            "roles",
                            "geometry",
                            "steps",
                            "exceptions",
                            "decision_authority",
                            "assumptions",
                            "risks",
                            "review_flags",
                            "support",
                            "feasibility",
                        },
                    ),
                    "counter_tactics": [
                        ct.model_dump(mode="json")
                        for ct in (p.review.counter_tactics if p.review else [])
                    ],
                }
                for p in self.parts
            ],
        }
        return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source_label,
            "selection": str(self.selection_path),
            "confirmed_by": self.doc.get("confirmed_by"),
            "date": str(self.doc.get("date") or ""),
            "user_notes": self.user_notes,
            "rejected": list(self.doc.get("rejected") or []),
            "parts": [
                {
                    "candidate": p.card.id,
                    "name": p.card.name,
                    "revision": p.card.revision,
                    "phases": "all" if p.whole else p.phases,
                    "provenance": p.card.provenance,
                    "feasibility": p.card.feasibility.level if p.card.feasibility else None,
                    "redteam": p.review.overall.soundness
                    if p.review and p.review.overall
                    else None,
                }
                for p in self.parts
            ],
            "reference_assumptions": [f"{c}: {a}" for c, a in self.reference_assumptions()],
            "counter_tactics": [
                f"{c}/{ct.id}: {ct.description}" for c, ct in self.counter_tactics()
            ],
        }


# ---------------------------------------------------------------------- loading
_PROVENANCE = {"retrieved": "检索", "adapted": "改编", "combined": "组合", "transferred": "迁移"}
_FEAS = {"ready": "可实现", "needs_confirmation": "需确认", "needs_development": "需开发"}
_LEVEL = {"low": "低", "medium": "中", "high": "高"}


def _fail(message: str, details: list[str] | None = None) -> FalconError:
    return FalconError("FAL003", message, details or [])


def load_selection(path: Path) -> SelectedTactic:
    """``path``: a ``tactic_selection.yaml`` or the Crow tactics directory holding it."""
    from afsim_research_agent.crow.annotate import load_result
    from afsim_research_agent.crow.mission import load_profile
    from afsim_research_agent.crow.selection_report import REVIEW_FILE, load_reviews

    selection_path = path / SELECTION_FILE if path.is_dir() else path
    directory = selection_path.parent
    try:
        doc = yaml.safe_load(selection_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise _fail(
            f"cannot read tactic selection {selection_path.name}",
            [
                str(exc),
                "先在 Crow 中选择战术：python -m afsim_research_agent.crow.select <战术目录> --candidate tc_01 --by <姓名>",
            ],
        ) from exc
    if not isinstance(doc, dict) or doc.get("format") != "TacticSelection":
        raise _fail(f"{selection_path.name} is not a TacticSelection file")
    if not doc.get("confirmed_by"):
        raise _fail("tactic selection has no confirmed_by")
    try:
        result = load_result(directory)
    except (OSError, ValidationError, AttributeError, TypeError) as exc:
        raise _fail(
            f"cannot read the candidate tactics next to {selection_path.name}",
            [f"{type(exc).__name__}: {str(exc)[:200]}"],
        ) from exc
    reviews = {}
    if (directory / REVIEW_FILE).is_file():
        reviews = {r.candidate: r for r in load_reviews(directory / REVIEW_FILE)[0]}
    cards = {c.id: c for c in result.candidates}
    problems: list[str] = []
    parts: list[SelectedPart] = []
    for item in doc.get("selected") or []:
        cid = str(item.get("candidate", ""))
        card = cards.get(cid)
        if card is None:
            problems.append(
                f"候选 {cid} 不在 tactic_candidates.yaml 中（可选：{', '.join(cards)}）"
            )
            continue
        revision = int(item.get("revision") or 1)
        if revision != card.revision:
            problems.append(
                f"{cid}：选择的是第 {revision} 版，候选现在是第 {card.revision} 版（选择后又被修改）；请重新选择"
            )
        phases = item.get("phases", "all")
        if phases in (None, "all"):
            chosen = None
        else:
            chosen = [str(p) for p in phases]
            known = [s.phase for s in card.steps]
            unknown = [p for p in chosen if p not in known]
            if unknown:
                problems.append(
                    f"{cid} 没有阶段 {'、'.join(unknown)}（它的阶段：{'、'.join(known)}）"
                )
        parts.append(SelectedPart(card, chosen, reviews.get(cid)))
    if not parts and not problems:
        problems.append("tactic_selection.yaml 没有选定任何候选")
    if problems:
        raise _fail("tactic selection does not match the candidate tactics", problems)
    profile = None
    if (directory / "mission_profile.yaml").is_file():
        try:
            profile = load_profile(directory / "mission_profile.yaml")
        except (OSError, ValidationError, yaml.YAMLError):
            profile = None
    return SelectedTactic(directory, selection_path, doc, result, parts, profile)


# ---------------------------------------------------------------------- review section
def render_section(tactic: SelectedTactic, number: str = "0b") -> list[str]:
    s = tactic.summary()
    lines = [
        f"## {number}. 选定战术（Crow C5）",
        "",
        f"- 来源：`{tactic.selection_path}`；选择人 {s['confirmed_by']}，{s['date'] or '—'}",
    ]
    if s["user_notes"]:
        lines.append(f"- 用户备注（已并入需求）：{s['user_notes']}")
    if s["rejected"]:
        lines.append(f"- 否决：{', '.join(s['rejected'])}")
    lines += [
        "",
        "| 候选 | 名称 | 版本 | 选用 | 来源 | 平台可行性 | 红方评价 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in s["parts"]:
        phases = "全部" if p["phases"] == "all" else "、".join(p["phases"])
        lines.append(
            f"| {p['candidate']} | {p['name']} | 第 {p['revision']} 版 | {phases} | "
            f"{_PROVENANCE.get(p['provenance'], p['provenance'])} | "
            f"{_FEAS.get(p['feasibility'] or '', '—')} | {_LEVEL.get(p['redteam'] or '', '—')} |"
        )
    lines += [
        "",
        "战术的步骤、例外和队形作为条款（编号 `tc_xx_s/x/g<n>`）列入 §1，必须被模型实现。",
    ]
    if s["reference_assumptions"]:
        lines += ["", "战术中的参考值（按参考值构建，见 §2 参考值清单复核）：", ""]
        lines += [f"- {a}" for a in s["reference_assumptions"]]
    if s["counter_tactics"]:
        lines += [
            "",
            f"红方反制 {len(s['counter_tactics'])} 项（本版仅记录，后续转为 L3 想定）：",
            "",
        ]
        lines += [f"- {c}" for c in s["counter_tactics"]]
    lines.append("")
    return lines
