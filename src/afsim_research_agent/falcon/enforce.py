"""basis rules enforced in code after the model answers (design §4 F4).

| case                                              | action                                   |
| ------------------------------------------------- | ---------------------------------------- |
| evidence id not in the package                    | removed                                  |
| cites evidence                                    | confirmation: pending                    |
| explicit, value outside every cited range         | -> inferred, rationale notes the range   |
| explicit, cited evidence has no numbers           | -> inferred                              |
| cites both sides of a recorded conflict           | -> conflicting (rationale required)      |
| user_specified not backed by a user-stated value  | -> unknown                               |
| no evidence, not user-stated                      | -> unknown, pending                      |
| requirement not met after the fixes               | -> unknown                               |
| a clause id cited as evidence                     | replaced by that clause's evidence       |
| rationale with template marks / over 300 chars    | marks removed / trimmed                  |
"""

from __future__ import annotations

import re
from typing import Any

from afsim_research_agent.evidence.merge import interval
from afsim_research_agent.falcon.models import (
    EnforcementNote,
    EvidencePackage,
    RequirementClause,
    UserValueRecord,
)
from afsim_research_agent.ir.units import PHYSICAL_TYPES, UnitError, compatible, to_canonical

ACCEPTED_BY = "falcon_request"
RATIONALE_MAX = 300
# the IR loader rejects these in any value (ir/loader.py _INTERPOLATION)
_TEMPLATE = re.compile(r"\$\{|\$\(|\{\{|\}\}|\{%|%\}")


class Enforcer:
    def __init__(
        self,
        package: EvidencePackage,
        user_values: list[UserValueRecord],
        clauses: list[RequirementClause],
        implements: dict[str, list[str]],
    ) -> None:
        self.package = package
        self.ids = {e.evidence_id for e in package.evidence}
        self.conflicts = package.conflict_pairs()
        self.user_values = user_values
        self.modelled = {c.id for c in clauses if c.kind != "out_of_scope"}
        # a clause id cited as evidence stands for that clause's evidence (tactic clauses)
        self.clause_evidence = {c.id: [e for e in c.evidence if e in self.ids] for c in clauses}
        self.implements = implements
        self.notes: list[EnforcementNote] = []

    def note(self, path: str, rule: str, detail: str) -> None:
        self.notes.append(EnforcementNote(path=path, rule=rule, detail=detail))

    # ------------------------------------------------------------------ helpers
    def _user_backed_value(self, param: dict[str, Any], name: str) -> bool:
        value, unit, kind = param.get("value"), param.get("unit"), param.get("type")
        if value is None:
            return False
        for uv in self.user_values:
            if kind in PHYSICAL_TYPES:
                if uv.canonical is None or not unit or uv.dimension is None:
                    continue
                if not compatible(uv.dimension, kind):
                    continue
                try:
                    mine = to_canonical(float(value), unit, kind)
                except (UnitError, TypeError, ValueError):
                    return False
                if abs(mine - uv.canonical) <= 1e-6 * max(1.0, abs(mine)):
                    return True
            elif uv.unit is None:
                if isinstance(value, bool) or isinstance(uv.value, bool):
                    if value == uv.value and uv.name == name:
                        return True
                elif isinstance(value, int | float) and isinstance(uv.value, int | float):
                    if float(value) == float(uv.value):
                        return True
                elif value == uv.value and uv.name == name:
                    return True
        return False

    def _cited_ranges(self, evidence: list[str], kind: str) -> list:
        out = []
        for eid in evidence:
            item = self.package.item(eid)
            if item is None or item.quantity is None:
                continue
            q = item.quantity
            if compatible(q.dimension, kind):
                out.append((eid, interval(q.min, q.max, q.unit, q.dimension)))
        return out

    def _cites_conflict(self, evidence: list[str]) -> bool:
        cited = set(evidence)
        return any(pair <= cited for pair in self.conflicts)

    def _rationale(self, value: Any, path: str) -> str | None:
        """Plain text only (no template characters the IR loader rejects), and short:
        a rationale is a reason, not the model thinking aloud."""
        text = str(value or "").strip()
        if not text:
            return None
        clean = _TEMPLATE.sub("", text).replace("`", "")
        if clean != text:
            self.note(path, "rationale_cleaned", "理由中的模板/代码标记已去掉")
        if len(clean) > RATIONALE_MAX:
            clean = clean[:RATIONALE_MAX].rstrip() + "…"
            self.note(path, "rationale_trimmed", f"理由过长，截取前 {RATIONALE_MAX} 字")
        return clean or None

    # ------------------------------------------------------------------ core
    def basis(
        self,
        basis: dict[str, Any] | None,
        path: str,
        *,
        element: str | None = None,
        param: dict[str, Any] | None = None,
        param_name: str | None = None,
    ) -> dict[str, Any]:
        b = dict(basis or {})
        if basis is None:
            self.note(path, "missing_basis", "缺少 basis，设为 unknown")
        status = b.get("status", "unknown")
        evidence = [e for e in b.get("evidence", []) or [] if isinstance(e, str)]
        cited_clauses = [e for e in evidence if e in self.clause_evidence]
        if cited_clauses:
            expanded = [x for e in cited_clauses for x in self.clause_evidence[e]]
            evidence = [e for e in evidence if e not in self.clause_evidence] + expanded
            self.note(
                path,
                "clause_as_evidence",
                f"条款编号 {cited_clauses} 不是证据，已换成这些条款的证据 {expanded or '（无）'}",
            )
        kept = [e for e in dict.fromkeys(evidence) if e in self.ids]
        if len(kept) != len(evidence):
            removed = sorted(set(evidence) - set(kept))
            self.note(path, "unknown_evidence", f"删除不存在的证据引用 {removed}")
        evidence = kept
        assumptions = list(b.get("assumptions", []) or [])
        rationale = self._rationale(b.get("rationale"), path)

        if status == "user_specified":
            if param is not None:
                ok = self._user_backed_value(param, param_name or "")
            else:
                ok = bool(set(self.implements.get(element or "", [])) & self.modelled)
            if not ok:
                self.note(
                    path, "user_specified_unbacked", "需求和请求文件中没有这个值，降为 unknown"
                )
                status = "unknown"

        if (
            status != "user_specified"
            and self._cites_conflict(evidence)
            and status != "conflicting"
        ):
            self.note(path, "cites_conflict", f"所引证据相互冲突，{status} 改为 conflicting")
            status = "conflicting"
            rationale = rationale or "所引证据给出相互冲突的说法，取值需人工裁决。"

        if (
            status == "explicit"
            and param is not None
            and param.get("type") in PHYSICAL_TYPES
            and param.get("value") is not None
            and param.get("unit")
        ):
            ranges = self._cited_ranges(evidence, str(param["type"]))
            try:
                value = to_canonical(float(param["value"]), str(param["unit"]), str(param["type"]))
            except (UnitError, TypeError, ValueError):
                value = None
            if value is not None and not ranges:
                status = "inferred"
                extra = "所引证据没有给出数值，数值为推断。"
                rationale = f"{rationale} {extra}" if rationale else extra
                self.note(path, "explicit_without_number", "所引证据没有数值，降为 inferred")
            elif value is not None and not any(r.contains(value) for _, r in ranges):
                spans = "、".join(f"[{r.low:g}, {r.high:g}]" for _, r in ranges)
                status = "inferred"
                extra = f"值 {param['value']} {param['unit']} 不在证据范围 {spans}（规范单位）内。"
                rationale = f"{rationale} {extra}" if rationale else extra
                self.note(path, "value_out_of_range", extra)

        if status == "explicit" and not evidence:
            self.note(path, "explicit_without_evidence", "explicit 没有有效证据，降为 unknown")
            status = "unknown"
        if status == "conflicting" and (len(evidence) < 2 or not rationale):
            self.note(
                path, "conflicting_incomplete", "conflicting 需要 ≥2 条证据和理由，降为 unknown"
            )
            status = "unknown"
        if status == "inferred" and (not rationale or not (evidence or assumptions)):
            self.note(path, "inferred_incomplete", "inferred 需要理由及证据或假设，降为 unknown")
            status = "unknown"

        out: dict[str, Any] = {"status": status}
        if evidence:
            out["evidence"] = evidence
        if assumptions:
            out["assumptions"] = assumptions
        if rationale:
            out["rationale"] = rationale
        if status == "user_specified":
            out["confirmation"] = "confirmed"
            out["confirmed_by"] = ACCEPTED_BY
        else:
            out["confirmation"] = "pending"
        return out

    # ------------------------------------------------------------------ walk
    def run(self, data: dict[str, Any]) -> None:
        for name, param in data.get("parameters", {}).items():
            param["basis"] = self.basis(
                param.get("basis"), f"parameters.{name}.basis", param=param, param_name=name
            )
        for key in ("formations", "coordination"):
            for index, item in enumerate(data.get(key, [])):
                item["basis"] = self.basis(
                    item.get("basis"), f"{key}[{index}].basis", element=item.get("id")
                )
        for index, item in enumerate(data.get("behaviors", [])):
            if "basis" in item:
                item["basis"] = self.basis(
                    item["basis"], f"behaviors[{index}].basis", element=item.get("id")
                )
        for r_index, role in enumerate(data.get("roles", [])):
            machine = role.get("state_machine") or {}
            for s_index, state in enumerate(machine.get("states", [])):
                for t_index, tr in enumerate(state.get("transitions", [])):
                    if tr.get("when") == {"literal": True}:
                        tr.pop("basis", None)
                        continue
                    tr["basis"] = self.basis(
                        tr.get("basis"),
                        f"roles[{r_index}].state_machine.states[{s_index}].transitions[{t_index}].basis",
                        element=tr.get("transition_id"),
                    )
        for assumption in data.get("assumptions", []):
            assumption["confirmation"] = "pending"
            assumption.pop("confirmed_by", None)


def enforce(
    data: dict[str, Any],
    package: EvidencePackage,
    user_values: list[UserValueRecord],
    clauses: list[RequirementClause],
    implements: dict[str, list[str]],
) -> list[EnforcementNote]:
    enforcer = Enforcer(package, user_values, clauses, implements)
    enforcer.run(data)
    return enforcer.notes
