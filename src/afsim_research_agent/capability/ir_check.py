"""K3: capability consistency check of a normalized IR against the platform catalog.

Integration point ⑤ (design/capability-catalog-integration-v1.md §3.3). Pure code: every
action, operand, function, blackboard use and role dependency in ``normalized_ir.json`` is
mapped to the catalog and judged with the same engine as Crow C4b, so gap IDs agree.

Outputs (written by the IR pipeline):

- ``capability_gaps.yaml`` in the model package (phase ``ir_check``), with where each gap is
  used (behaviors, conditions, transitions, tree nodes, roles), acceptance cases (L2 tests,
  L3 scenarios), alternatives, the user's decision and the Crow estimate it confirms;
- a section of ``review.md``;
- comments in the BehaviorTree.CPP XML before every node that needs a missing capability.

Gaps never block L1/L2/L3 or the existing backends; only AFSIM backend generation requires
every blocking gap to have a decision (``capability_decisions.yaml``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from afsim_research_agent.capability.assess import (
    GAP_TYPES,
    LABELS,
    RANK,
    Aliases,
    Verdict,
    assess,
    assess_ir_element,
    gap_id,
    map_binding,
    summarize,
)
from afsim_research_agent.capability.catalog import CapabilityCatalog, CatalogError, Interface
from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.capability.ir_elements import used_elements

CHECK_VERSION = "ir-capability-check-2026.10.1"
GAPS_FILE = "capability_gaps.yaml"
DECISIONS_FILE = "capability_decisions.yaml"
# Crow's estimated gaps of the tactic the package implements (Falcon P4 writes it)
CROW_GAPS_FILE = "crow_capability_gaps.yaml"
NEEDS_FILE = "capability_needs.yaml"  # Falcon v2 sheet: capabilities outside the vocabulary
GATED_BACKENDS = frozenset({"afsim"})
Decision = Literal["confirmed", "alternative", "pending_plugin"]
DECISION_LABELS = {
    "confirmed": "已确认平台支持",
    "alternative": "接受替代写法",
    "pending_plugin": "待插件开发",
}
_GAP_ORDER = {"develop": 2, "ir_extension": 1, "confirm": 0}


# ---------------------------------------------------------------------- result models
@dataclass
class ElementCheck:
    """One IR element (or role dependency) judged against the catalog."""

    name: str
    kind: str
    mapping: str
    conclusion: str
    refs: list[str] = field(default_factory=list)
    alternatives: list[tuple[list[str], str]] = field(default_factory=list)
    chosen: int | None = None
    reasons: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    gap_kinds: dict[str, str] = field(default_factory=dict)
    group: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.conclusion]

    @property
    def gap_type(self) -> str | None:
        return GAP_TYPES.get(self.conclusion)


class IrGap(BaseModel):
    id: str
    contract: str = Field(description="Catalog reference, or the IR element when unmapped.")
    display_name: str
    gap_type: Literal["confirm", "ir_extension", "develop"]
    blocking: bool = True
    elements: list[str] = Field(default_factory=list, description="IR elements needing it.")
    used_at: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    tree_nodes: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    source: str = "ir_check"
    crow_candidates: list[str] = Field(default_factory=list)
    new_in_ir: bool = False
    decision: Decision | None = None
    decision_note: str | None = None
    status: str = "open"


class SimplifiedItem(BaseModel):
    """Missing or unconfirmed, outside the precisely checked groups: simplified, not a gap."""

    element: str
    group: str | None = None
    group_name: str = ""
    simplification: str = ""
    roles: list[str] = Field(default_factory=list)


class CrowGapStatus(BaseModel):
    id: str
    contract: str
    display_name: str
    gap_type: str
    in_ir: Literal["confirmed", "resolved", "not_used"]
    note: str = ""


class ElementRow(BaseModel):
    name: str
    kind: str
    conclusion: str
    label: str
    refs: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    note: str = ""


class CapabilityCheck(BaseModel):
    format: str = "CapabilityGaps"
    format_version: str = "0.2.0"
    phase: str = "ir_check"
    check_version: str = CHECK_VERSION
    catalog_id: str
    catalog_version: str
    aliases_version: str
    ir_hash: str | None = None
    granularity: dict[str, Any] = Field(default_factory=lambda: {"granularity": "fine"})
    level: Literal["ready", "needs_confirmation", "needs_development"]
    counts: dict[str, int] = Field(default_factory=dict)
    unresolved: list[str] = Field(
        default_factory=list, description="Blocking gaps without a decision."
    )
    gaps: list[IrGap] = Field(default_factory=list)
    simplified: list[SimplifiedItem] = Field(default_factory=list)
    crow_estimate: list[CrowGapStatus] = Field(default_factory=list)
    crow_source: str | None = None
    elements: list[ElementRow] = Field(default_factory=list)
    node_gaps: dict[str, list[str]] = Field(
        default_factory=dict, description="Behavior-tree node name → gap IDs."
    )

    @property
    def afsim_ready(self) -> bool:
        return not self.unresolved

    def withheld(self) -> list[str]:
        """IR elements a faithful simulation must not provide: actions and operand
        properties whose gaps are all still unresolved (simplified items stay ideal)."""
        open_ids = set(self.unresolved)
        return sorted(
            e.name
            for e in self.elements
            if e.kind in ("action", "operand") and e.gaps and set(e.gaps) <= open_ids
        )

    def summary(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "catalog_version": self.catalog_version,
            "gaps": len(self.gaps),
            "simplified": len(self.simplified),
            **self.granularity,
            "unresolved": list(self.unresolved),
            "afsim_ready": self.afsim_ready,
            **({"crow_source": self.crow_source} if self.crow_source else {}),
        }

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json", exclude={"node_gaps"})
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


# ---------------------------------------------------------------------- IR structure helpers
def _walk(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _condition_refs(node: Any) -> set[str]:
    return {n["id"] for n in _walk(node) if n.get("op") == "condition_ref" and "id" in n}


_INDEXED = re.compile(r"^(behaviors|conditions|coordination|trees)\[(\d+)\]")
_TRANSITION = re.compile(r"^roles\[(\d+)\]\.state_machine\.states\[(\d+)\]\.transitions\[(\d+)\]")
_DEPENDENCY = re.compile(r"^roles\.([^.]+)\.dependencies")


def _owner(ir: dict[str, Any], location: str) -> str | None:
    if m := _INDEXED.match(location):
        section, index = m.group(1), int(m.group(2))
        prefix = {"behaviors": "behavior", "conditions": "condition"}.get(section, section[:-1])
        if section == "coordination":
            prefix = "coordination"
        return f"{prefix}:{ir[section][index]['id']}"
    if m := _TRANSITION.match(location):
        role = ir["roles"][int(m.group(1))]
        state = role["state_machine"]["states"][int(m.group(2))]
        return f"transition:{state['transitions'][int(m.group(3))]['transition_id']}"
    if m := _DEPENDENCY.match(location):
        return f"role:{m.group(1)}"
    return None


def _tree_nodes(tree: dict[str, Any]) -> Iterable[dict[str, Any]]:
    stack = [tree["root"]]
    while stack:
        node = stack.pop(0)
        yield node
        stack += node.get("children", [])


@dataclass
class _Structure:
    """Who uses what: owners per role, condition references, tree nodes per owner."""

    roles_of: dict[str, set[str]]
    refs_of: dict[str, set[str]]
    nodes_of: dict[str, list[str]]
    transitions_of: dict[str, list[str]]
    classes: dict[str, str]


def _structure(ir: dict[str, Any]) -> _Structure:
    from afsim_research_agent.ir.backends.common import trees_for_role

    trees = {t["id"]: t for t in ir.get("trees", [])}
    refs_of: dict[str, set[str]] = {}
    for b in ir.get("behaviors", []):
        refs_of[f"behavior:{b['id']}"] = _condition_refs(b)
    for c in ir.get("conditions", []):
        refs_of[f"condition:{c['id']}"] = _condition_refs(c.get("expression"))
    nodes_of: dict[str, list[str]] = {}
    for tree in trees.values():
        for node in _tree_nodes(tree):
            for key, prefix in (("behavior", "behavior"), ("condition", "condition")):
                if node.get(key):
                    nodes_of.setdefault(f"{prefix}:{node[key]}", []).append(node["node_id"])
    roles_of: dict[str, set[str]] = {}
    transitions_of: dict[str, list[str]] = {}
    classes = {}
    for role in ir.get("roles", []):
        rid = role["id"]
        classes[rid] = role["capability_class"]
        direct: set[str] = {f"role:{rid}"}
        for tree_id in trees_for_role(ir, role):
            for node in _tree_nodes(trees[tree_id]):
                if node.get("behavior"):
                    direct.add(f"behavior:{node['behavior']}")
                if node.get("condition"):
                    direct.add(f"condition:{node['condition']}")
        for state in (role.get("state_machine") or {}).get("states", []):
            for t in state.get("transitions", []):
                owner = f"transition:{t['transition_id']}"
                direct.add(owner)
                transitions_of.setdefault(rid, []).append(owner)
                refs_of[owner] = _condition_refs(t.get("when"))
        for owner in _closure(direct, refs_of):
            roles_of.setdefault(owner, set()).add(rid)
    for entry in ir.get("coordination", []):
        text = json.dumps(entry, ensure_ascii=False)
        roles_of[f"coordination:{entry['id']}"] = {
            r["id"] for r in ir.get("roles", []) if re.search(rf"\b{re.escape(r['id'])}\b", text)
        }
    return _Structure(roles_of, refs_of, nodes_of, transitions_of, classes)


def _closure(owners: Iterable[str], refs_of: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(owners)
    while stack:
        owner = stack.pop()
        if owner in seen:
            continue
        seen.add(owner)
        stack += [f"condition:{c}" for c in refs_of.get(owner, ())]
    return seen


# ---------------------------------------------------------------------- judgment
def _worst_verdict(
    catalog: CapabilityCatalog, classes: Iterable[str], judge: Any
) -> tuple[Any, Verdict]:
    results = [judge(cls) for cls in sorted(set(classes)) or [None]]
    return max(results, key=lambda r: RANK[r[1].conclusion])


def _element_gaps(name: str, verdict: Verdict) -> dict[str, str]:
    """Gap ID → gap type, from the references that make the verdict a gap."""
    if verdict.gap_type is None:
        return {}
    refs = verdict.blocking_refs()
    if not refs:
        return {gap_id(f"ir:{name}"): verdict.gap_type}
    return {gap_id(r.ref): GAP_TYPES[r.conclusion] for r in refs}


def _check_element(
    catalog: CapabilityCatalog,
    aliases: Aliases,
    name: str,
    kind: str,
    classes: set[str],
    policy: CapabilityPolicy | None = None,
) -> ElementCheck:
    if kind == "dependency":
        binding = name.split(":", 1)[1]
        uses = map_binding(binding, aliases)
        static = binding in (aliases or Aliases.default()).static_bindings

        def judge(cls: str | None) -> tuple[None, Verdict]:
            if static:
                return None, Verdict("ready", reasons=["由行为模型运行时提供"])
            if not uses:
                if policy is not None and policy.simplifies(None, cls, True):
                    return None, Verdict("simplified", reasons=[policy.simplification(None, True)])
                return None, Verdict("develop", reasons=["对照表中没有该角色依赖"])
            return None, assess(catalog, uses, (), cls, policy=policy)

        _, verdict = _worst_verdict(catalog, classes, judge)
        mapping = "static" if static else ("alias" if uses else "unmapped")
        refs, any_of = list(uses), []
    else:
        found, verdict = _worst_verdict(
            catalog, classes, lambda cls: assess_ir_element(catalog, name, cls, aliases, policy)
        )
        mapping = found.kind
        refs, any_of = list(found.uses), [list(a) for a in found.any_of]
    alternatives = [
        (alt, max((r.conclusion for r in res), key=RANK.__getitem__, default="develop"))
        for alt, res in zip(any_of, verdict.alternatives, strict=False)
    ]
    kinds = _element_gaps(name, verdict)
    return ElementCheck(
        name=name,
        kind=kind,
        mapping=mapping,
        conclusion=verdict.conclusion,
        refs=refs,
        alternatives=alternatives,
        chosen=verdict.chosen_alternative,
        reasons=list(dict.fromkeys(verdict.reasons)),
        gaps=list(kinds),
        gap_kinds=kinds,
        group=_group(policy, kind, name, refs + [r for a in any_of for r in a]),
    )


def _group(policy: CapabilityPolicy | None, kind: str, name: str, refs: list[str]) -> str | None:
    policy = policy or CapabilityPolicy.make("fine")
    if kind == "dependency":
        return next((g for r in refs if (g := policy.table.group_of(r))), None)
    return policy.ir_group(name, refs)


def _display(catalog: CapabilityCatalog, contract: str) -> str:
    try:
        target = catalog.resolve(contract)
    except CatalogError:
        return contract
    if isinstance(target, Interface):
        cap = catalog.get(target.capability_id)
        return f"{cap.display_name if cap else target.capability_id} · {target.display_name}"
    return target.display_name


# ---------------------------------------------------------------------- inputs
def load_policy(
    package_dir: Path, granularity: str | None = None, refine: Iterable[str] | None = None
) -> CapabilityPolicy:
    """``granularity`` / ``refine`` from the arguments, else ``capability_decisions.yaml``,
    else coarse (a first draft)."""
    data: dict[str, Any] = {}
    path = package_dir / DECISIONS_FILE
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return CapabilityPolicy.make(
        granularity or data.get("granularity"),
        refine if refine is not None else data.get("refine") or (),
    )


def decisions_template(policy: CapabilityPolicy) -> str:
    """Initial ``capability_decisions.yaml`` for a new model package."""
    groups = "、".join(f"{g.id}（{g.display_name}）" for g in policy.table.groups.values())
    return (
        "# 平台能力处理（由用户编辑；ir build 读取）\n"
        "# granularity: coarse = 只精确核对核心能力组，其余缺失或未确认时简化处理；\n"
        "#              fine = 全部精确核对\n"
        f"# refine: 需要精确核对（含细节项）的能力组，可选：{groups}\n"
        "# decisions: 缺口编号 → {decision: confirmed | alternative | pending_plugin, note: ...}\n"
        + yaml.safe_dump(
            {"granularity": policy.granularity, "refine": sorted(policy.refine), "decisions": {}},
            allow_unicode=True,
            sort_keys=False,
        )
    )


def load_decisions(package_dir: Path) -> dict[str, dict[str, Any]]:
    """``capability_decisions.yaml``: ``decisions: {gap_id: {decision, note}}``."""
    path = package_dir / DECISIONS_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, Any]] = {}
    for gid, entry in (data.get("decisions") or {}).items():
        entry = entry if isinstance(entry, dict) else {"decision": entry}
        if entry.get("decision") in DECISION_LABELS:
            out[str(gid)] = entry
    return out


def load_crow_gaps(path: Path, candidate: str | None = None) -> list[dict[str, Any]]:
    """Crow C4b ``capability_gaps.yaml``; only the gaps of ``candidate`` when given."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gaps = data.get("gaps") or []
    if candidate:
        gaps = [
            g for g in gaps if any(u.get("candidate") == candidate for u in g.get("used_by", []))
        ]
    return gaps


def load_sheet_needs(package_dir: Path) -> list[dict[str, Any]] | None:
    """``capability_needs.yaml`` written by Falcon v2 (None when absent)."""
    path = package_dir / NEEDS_FILE
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [n for n in data.get("needs") or [] if isinstance(n, dict)]


# ---------------------------------------------------------------------- the check
def check_ir(
    ir: dict[str, Any],
    catalog: CapabilityCatalog,
    *,
    aliases: Aliases | None = None,
    decisions: dict[str, dict[str, Any]] | None = None,
    crow_gaps: list[dict[str, Any]] | None = None,
    crow_source: str | None = None,
    policy: CapabilityPolicy | None = None,
    sheet_needs: list[dict[str, Any]] | None = None,
) -> CapabilityCheck:
    """``policy``: coarse / fine judgment (``granularity.py``); None judges everything
    precisely. ``sheet_needs``: the tactic sheet's ``needs`` (``capability_needs.yaml``),
    capabilities the model needs but the vocabulary lacks; each becomes a gap."""
    aliases = aliases or Aliases.default()
    decisions = decisions or {}
    structure = _structure(ir)
    elements: list[ElementCheck] = []
    for name, use in sorted(used_elements(ir).items()):
        owners = sorted({o for loc in use.locations if (o := _owner(ir, loc))})
        roles = sorted({r for o in owners for r in structure.roles_of.get(o, ())})
        classes = {structure.classes[r] for r in roles} or set(use.platform_classes)
        check = _check_element(catalog, aliases, name, use.kind, classes, policy)
        check.owners, check.roles = owners, roles
        elements.append(check)

    # gaps per owner, then through condition references to tree nodes and transitions
    own: dict[str, set[str]] = {}
    for e in elements:
        for owner in e.owners:
            own.setdefault(owner, set()).update(e.gaps)

    def effective(owner: str) -> set[str]:
        return set().union(*(own.get(o, set()) for o in _closure([owner], structure.refs_of)))

    node_gaps: dict[str, list[str]] = {}
    for owner, nodes in structure.nodes_of.items():
        gids = effective(owner)
        for node in nodes:
            if gids:
                node_gaps[node] = sorted(set(node_gaps.get(node, [])) | gids)
    for role, transitions in structure.transitions_of.items():
        gids = set().union(*(effective(t) for t in transitions))
        if gids:
            node_gaps[f"{role}_transitions"] = sorted(gids)

    gaps: dict[str, IrGap] = {}
    for e in elements:
        refs_pool = list(e.refs) + [r for alt, _ in e.alternatives for r in alt]
        for gid in e.gaps:
            contract = next((r for r in refs_pool if gap_id(r) == gid), None)
            if contract is None:
                contract = f"ir:{e.name}"
            gap_type = e.gap_kinds[gid]
            gap = gaps.get(gid)
            if gap is None:
                gap = gaps[gid] = IrGap(
                    id=gid,
                    contract=contract,
                    display_name=_display(catalog, contract),
                    gap_type=gap_type,  # type: ignore[arg-type]
                )
            elif _GAP_ORDER[gap_type] > _GAP_ORDER[gap.gap_type]:
                gap.gap_type = gap_type  # type: ignore[assignment]
            _extend(gap.elements, [e.name])
            _extend(gap.used_at, e.owners)
            _extend(gap.roles, e.roles)
            for i, (alt, conclusion) in enumerate(e.alternatives):
                if i != e.chosen:
                    _extend(gap.alternatives, [f"{' + '.join(alt)}（{LABELS[conclusion]}）"])
    for node, gids in node_gaps.items():
        for gid in gids:
            if gid in gaps:
                _extend(gaps[gid].tree_nodes, [node])
    for need in sheet_needs or []:
        gid = str(need.get("gap_id") or gap_id(str(need.get("need", ""))))
        gap_type = str(need.get("gap_type") or "develop")
        if gap_type not in _GAP_ORDER:
            gap_type = "develop"
        gap = gaps.get(gid)
        if gap is None:
            gap = gaps[gid] = IrGap(
                id=gid,
                contract=str(need.get("contract") or f"need:{need.get('need', '')}"),
                display_name=str(need.get("display_name") or need.get("need") or gid),
                gap_type=gap_type,  # type: ignore[arg-type]
                source="falcon_sheet",
            )
        else:
            gap.source = f"{gap.source}+falcon_sheet"
        _extend(gap.elements, [f"need:{need.get('id')}"])
        _extend(gap.used_at, [str(u) for u in need.get("used_by") or []])
        _extend(gap.roles, [str(r) for r in need.get("roles") or []])
        if need.get("substitute"):
            _extend(gap.alternatives, [f"{need['substitute']}（战术稿替代写法）"])
    for gap in gaps.values():
        gap.acceptance = _acceptance(ir, gap)
        decision = decisions.get(gap.id)
        if decision:
            gap.decision = decision["decision"]
            gap.decision_note = decision.get("note")
            gap.status = "decided"

    crow_rows: list[CrowGapStatus] = []
    if crow_gaps is not None:
        used_refs = {r for e in elements for r in e.refs} | {
            r for e in elements for alt, _ in e.alternatives for r in alt
        }
        crow_ids = set()
        for g in crow_gaps:
            gid = str(g.get("id"))
            crow_ids.add(gid)
            candidates = [u.get("candidate") for u in g.get("used_by", []) if u.get("candidate")]
            if gid in gaps:
                sheet = "falcon_sheet" in gaps[gid].source
                gaps[gid].source = "crow_estimate+" + ("falcon_sheet" if sheet else "ir_check")
                _extend(gaps[gid].crow_candidates, candidates)
                state, note = "confirmed", ("战术稿登记为词表外需求" if sheet else "IR 中同样需要")
            elif g.get("contract") in used_refs:
                state, note = "resolved", "IR 使用了该能力，但按 IR 的用法不构成缺口"
            else:
                state, note = "not_used", "IR 未使用该能力（实现方式不同，或该要素未建模）"
            crow_rows.append(
                CrowGapStatus(
                    id=gid,
                    contract=str(g.get("contract", "")),
                    display_name=str(g.get("display_name", g.get("contract", ""))),
                    gap_type=str(g.get("gap_type", "")),
                    in_ir=state,  # type: ignore[arg-type]
                    note=note,
                )
            )
        for gap in gaps.values():
            gap.new_in_ir = gap.id not in crow_ids

    ordered = sorted(gaps.values(), key=lambda g: (-_GAP_ORDER[g.gap_type], -len(g.used_at), g.id))
    feasibility = summarize(("required", _as_verdict(e), e.gaps) for e in elements)
    level = feasibility.level
    counts = {k: v for k, v in feasibility.counts.items() if v}
    sheet_gaps = [g for g in gaps.values() if "falcon_sheet" in g.source]
    if sheet_gaps:
        counts["sheet_needs"] = len(sheet_gaps)
        if any(g.gap_type in ("develop", "ir_extension") for g in sheet_gaps):
            level = "needs_development"
        elif level == "ready":
            level = "needs_confirmation"
    unresolved = [g.id for g in ordered if g.blocking and g.decision is None]
    table = (policy or CapabilityPolicy.make("fine")).table
    simplified = [
        SimplifiedItem(
            element=e.name,
            group=e.group,
            group_name=table.groups[e.group].display_name if e.group in table.groups else "",
            simplification=_simplification_text(e, table),
            roles=e.roles,
        )
        for e in elements
        if e.conclusion == "simplified"
    ]
    return CapabilityCheck(
        catalog_id=catalog.catalog_id,
        catalog_version=catalog.content_hash,
        aliases_version=aliases.version,
        ir_hash=ir.get("ir_hash"),
        granularity=policy.summary() if policy else {"granularity": "fine", "refine": []},
        simplified=simplified,
        level=level,  # type: ignore[arg-type]
        counts=counts,
        unresolved=unresolved,
        gaps=ordered,
        crow_estimate=crow_rows,
        crow_source=crow_source,
        elements=[_row(e) for e in elements],
        node_gaps=dict(sorted(node_gaps.items())),
    )


def _simplification_text(e: ElementCheck, table: Any) -> str:
    group = table.groups.get(e.group or "")
    texts = [r for r in e.reasons if "：" in r] or list(e.reasons)
    if group is not None:
        texts = [t.removeprefix(f"{group.display_name}：") for t in texts]
    return "；".join(dict.fromkeys(texts))


def _as_verdict(e: ElementCheck) -> Verdict:
    return Verdict(e.conclusion)


def _row(e: ElementCheck) -> ElementRow:
    refs = list(e.refs)
    if e.chosen is not None and e.alternatives:
        refs += e.alternatives[e.chosen][0]
    note = "；".join(e.reasons)
    if e.alternatives and e.chosen is not None:
        others = len(e.alternatives) - 1
        if others:
            note = "；".join(filter(None, [note, f"另有 {others} 种替代写法"]))
    return ElementRow(
        name=e.name,
        kind=e.kind,
        conclusion=e.conclusion,
        label=e.label,
        refs=refs,
        roles=e.roles,
        gaps=e.gaps,
        note=note,
    )


def _extend(target: list[str], values: Iterable[str]) -> None:
    for v in values:
        if v not in target:
            target.append(v)


def _acceptance(ir: dict[str, Any], gap: IrGap) -> list[str]:
    """L2 tests that exercise a node or action needing the gap; L3 scenarios with its roles."""
    nodes, actions = set(gap.tree_nodes), set(gap.elements)
    out = []
    for test in ir.get("tests", []):
        expect = test.get("expect") or {}
        if nodes & set(expect.get("selected_path") or []) or actions & set(
            expect.get("issued_actions") or []
        ):
            out.append(f"L2:{test['id']}")
    for scenario in ir.get("scenarios", []):
        if set(gap.roles) & set((scenario.get("initial") or {}).keys()):
            out.append(f"L3:{scenario['id']}")
    return out


# ---------------------------------------------------------------------- outputs
def afsim_gate(check: CapabilityCheck | None, backend: str) -> list[str]:
    """Gap IDs that stop ``backend``; only AFSIM backends are gated."""
    if check is None or not any(backend.startswith(g) for g in GATED_BACKENDS):
        return []
    return list(check.unresolved)


_NAME = re.compile(r'^(\s*)<(\w+)\b[^>]*\bname="([^"]+)"')


def annotate_btcpp_xml(text: str, check: CapabilityCheck) -> str:
    """Insert a comment before every node that needs a missing capability.

    Comments rather than attributes: BehaviorTree.CPP rejects XML attributes that are not
    declared ports, and the tree must stay loadable while gaps are open."""
    if not check.node_gaps:
        return text
    gaps = {g.id: g for g in check.gaps}
    out = []
    for line in text.splitlines(keepends=True):
        m = _NAME.match(line)
        if m and m.group(3) in check.node_gaps:
            ids = check.node_gaps[m.group(3)]
            status = " ".join(f"gap:{i}" for i in ids)
            names = "；".join(
                f"{LABELS[_conclusion(gaps[i].gap_type)]} {gaps[i].display_name}"
                for i in ids
                if i in gaps
            )
            comment = f'capability_status="{status}" {names}'.replace("--", "－")
            out.append(f"{m.group(1)}<!-- {comment} -->\n")
        out.append(line)
    return "".join(out)


def _conclusion(gap_type: str) -> str:
    return {"confirm": "confirm", "ir_extension": "ir_extension"}.get(gap_type, "develop")


_LEVEL = {"ready": "可实现", "needs_confirmation": "需确认", "needs_development": "需开发"}
_GAP_LABEL = {"confirm": "需确认", "ir_extension": "需扩展 IR", "develop": "需开发"}
_CROW_STATE = {"confirmed": "IR 中仍需要", "resolved": "IR 中不构成缺口", "not_used": "IR 未使用"}


def render_section(check: CapabilityCheck, number: int = 8) -> list[str]:
    """Markdown section for review.md."""
    lines = [f"## {number}. 平台能力核对", ""]
    lines.append(
        f"结论：**{_LEVEL[check.level]}**；缺口 {len(check.gaps)} 项，"
        f"未处理 {len(check.unresolved)} 项。缺口不影响 L1/L2/L3 与现有后端，"
        "生成 AFSIM 后端前需在 `capability_decisions.yaml` 中逐项处理"
        "（confirmed / alternative / pending_plugin）。"
    )
    level = check.granularity.get("granularity", "fine")
    refine = check.granularity.get("refine") or []
    scope = (
        "粗粒度：只精确核对核心能力组"
        + (f"及细化的 {', '.join(refine)}" if refine else "")
        + "，其余缺失或未确认时简化处理"
        if level == "coarse"
        else "细粒度：全部精确核对"
    )
    lines += [
        "",
        f"核对粒度：{scope}。目录 `{check.catalog_id}`（{check.catalog_version[:19]}…）。",
        "",
    ]
    if check.gaps:
        lines += [
            "| 缺口 | 能力 | 类型 | IR 元素 | 角色 | 行为树节点 | 处理 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for g in check.gaps:
            nodes = ", ".join(g.tree_nodes[:4]) + (" …" if len(g.tree_nodes) > 4 else "")
            decision = DECISION_LABELS.get(g.decision or "", "未处理")
            origin = "" if not check.crow_source else (" ★新" if g.new_in_ir else "")
            if "falcon_sheet" in g.source:
                origin += " ☆稿"
            lines.append(
                f"| `{g.id}`{origin} | {g.display_name} | {_GAP_LABEL[g.gap_type]} | "
                f"{', '.join(g.elements)} | {', '.join(g.roles) or '—'} | {nodes or '—'} | "
                f"{decision} |"
            )
        lines.append("")
        if check.crow_source:
            lines.append("★新：Crow 预估中没有、IR 核对新发现的缺口。")
            lines.append("")
        if any("falcon_sheet" in g.source for g in check.gaps):
            lines.append(
                "☆稿：战术稿 `yaml needs` 登记的词表外能力（"
                f"`{NEEDS_FILE}`），行为树中暂用替代写法。"
            )
            lines.append("")
    if check.simplified:
        lines += [
            f"**简化处理 {len(check.simplified)} 项**（不计为缺口；需要时在 "
            "`capability_decisions.yaml` 的 `refine` 中加入该能力组即可精确核对）",
            "",
            "| IR 元素 | 能力组 | 简化方式 | 角色 |",
            "| --- | --- | --- | --- |",
        ]
        for item in check.simplified:
            lines.append(
                f"| `{item.element}` | {item.group_name or item.group or '—'} | "
                f"{item.simplification or '—'} | {', '.join(item.roles) or '—'} |"
            )
        lines.append("")
    if check.crow_estimate:
        lines += [
            "**与 Crow 预估对照**",
            "",
            "| 缺口 | 能力 | Crow 类型 | IR 结果 |",
            "| --- | --- | --- | --- |",
        ]
        for c in check.crow_estimate:
            lines.append(
                f"| `{c.id}` | {c.display_name} | {_GAP_LABEL.get(c.gap_type, c.gap_type)} | "
                f"{_CROW_STATE[c.in_ir]} |"
            )
        lines.append("")
    lines += [
        "<details><summary>逐项对照</summary>",
        "",
        "| IR 元素 | 类别 | 结论 | 目录条目 | 角色 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in check.elements:
        refs = ", ".join(e.refs) or "—"
        lines.append(
            f"| `{e.name}` | {e.kind} | {e.label} | {refs} | {', '.join(e.roles) or '—'} | "
            f"{(e.note or '—').replace('|', '／')} |"
        )
    lines += ["", "</details>", ""]
    return lines
