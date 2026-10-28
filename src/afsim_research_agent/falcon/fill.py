"""F4: section-by-section generation.

Sections are generated in a fixed order; each prompt carries the skeleton,
the sections already done, the catalog slice the section needs and the
relevant claims. Coordination patterns are expanded after the ``structure``
section so later sections can reference the generated IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from afsim_research_agent.capability.constraints import CONSTRAINT_NOTE, VocabularyConstraints
from afsim_research_agent.falcon.assemble import assemble, dump_model, generated_ids
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import (
    SECTION_MODELS,
    SECTIONS,
    EvidencePackage,
    FalconRequest,
    RequirementClause,
    SkeletonDraft,
    UserValueRecord,
)
from afsim_research_agent.falcon.prompts import (
    catalog_actions,
    catalog_operands,
    catalog_patterns,
    section_prompt,
)
from afsim_research_agent.falcon.skeleton import clause_data, compact_evidence
from afsim_research_agent.ir.catalog import Catalog

OBSERVABLES = (
    "role_state、formation_error_rms、min_interval、max_interval、messages_sent、actions_issued、"
    "hostile_range_min、speed（每个观测量可带 role、instance、formation、message、command、state；"
    "role_state、speed、actions_issued、min_interval、max_interval 必须写 role，formation_error_rms 必须写 formation，"
    "messages_sent 必须写 message（协同ID.消息），actions_issued 必须写 command（动作节点名））"
)

# evidence topics shown to each section (None = no claims in the prompt)
SECTION_TOPICS: dict[str, set[str] | None] = {
    "parameters": None,  # all
    "structure": {"formation", "spacing", "bounding", "contact", "communication"},
    "conditions": None,
    "behaviors": None,
    "control": {"transition", "contact", "formation", "bounding", "terrain"},
    "verification": None,
}
SHOW_CLAIMS = {"parameters", "structure", "control"}


@dataclass
class DraftState:
    request: FalconRequest
    clauses: list[RequirementClause]
    user_values: list[UserValueRecord]
    package: EvidencePackage
    skeleton: SkeletonDraft
    catalog: Catalog
    capabilities: set[str] | None = None
    constraints: VocabularyConstraints | None = None
    sections: dict[str, BaseModel] = field(default_factory=dict)
    generated: dict[str, Any] | None = None
    tactic_block: str = ""  # P4: the selected Crow tactic, shown to every section

    def clause_data(self) -> list[dict[str, Any]]:
        return clause_data(self.clauses, {"id", "text", "kind"})

    def user_value_data(self) -> list[dict[str, Any]]:
        return [v.model_dump(include={"name", "value", "unit", "origin"}) for v in self.user_values]


def catalog_text(
    section: str, catalog: Catalog, constraints: VocabularyConstraints | None = None
) -> str:
    if section == "parameters":
        return "单位：distance m/km，speed m/s、km/h、kt，time s/sec/min/h，angle deg，ratio ratio/percent"
    if section == "structure":
        return catalog_patterns(catalog)
    note = f"{CONSTRAINT_NOTE}\n" if constraints is not None else ""
    operands = catalog_operands(catalog, constraints)
    if section == "conditions":
        return note + operands
    if section in ("behaviors", "control"):
        return note + "动作：\n" + catalog_actions(catalog, constraints) + "\n操作数：\n" + operands
    return note + f"L3 观测量：{OBSERVABLES}\n操作数：\n" + operands


def done_sections(state: DraftState, current: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in SECTIONS:
        if name == current:
            continue
        model = state.sections.get(name)
        if model is not None:
            out[name] = dump_model(model)
    return out


def available_ids(state: DraftState, section: str) -> dict[str, Any]:
    """IDs other sections already define, so this section references only real ones."""
    others = {k: v for k, v in state.sections.items() if k != section}
    data = assemble(state.request, state.skeleton, others, state.package).data
    gen = state.generated or {}
    roles = {}
    for role in data["roles"]:
        count = int(role.get("count", 1))
        machine = role.get("state_machine") or {}
        roles[role["id"]] = {
            "count": count,
            "instances": [f"{role['id']}[{i}]" for i in range(count)],
            "states": [st["id"] for st in machine.get("states", [])],
            "blackboard": [
                f"{board['id']}.{var['name']}"
                for board in role.get("blackboards", [])
                for var in board.get("variables", [])
            ],
        }
    trees = sorted({st.tree for r in state.skeleton.roles for st in r.states if st.tree})
    out: dict[str, Any] = {
        "roles": roles,
        "parameters": sorted(data["parameters"]),
        "formations": [f["id"] for f in data["formations"]],
        "coordination": [{"id": c["id"], "pattern": c.get("pattern")} for c in data["coordination"]]
        or [{"id": c.id, "pattern": c.pattern} for c in state.skeleton.coordination],
        "conditions": [c["id"] for c in data["conditions"]] + gen.get("conditions", []),
        "behaviors": [b["id"] for b in data["behaviors"]] + gen.get("behaviors", []),
        "messages": [m["id"] for m in data["messages"]] + gen.get("messages", []),
        "trees": trees + gen.get("trees", []),
    }
    if gen.get("usage"):
        out["generated_usage"] = gen["usage"]
    sync = {}
    for coord in data["coordination"]:
        if coord.get("pattern") == "leader_follower":
            members = [coord.get("leader"), *_names(coord.get("followers"))]
        elif coord.get("pattern") == "synchronized_transition":
            members = [coord.get("initiator"), *_names(coord.get("participants"))]
        else:
            continue
        sets = [set(roles[m]["states"]) for m in members if m in roles]
        if sets:
            sync[coord["id"]] = sorted(set.intersection(*sets))
    if sync:
        out["sync_targets"] = sync
    if section == "verification":
        out["transitions"] = [
            tr["transition_id"]
            for role in data["roles"]
            for st in (role.get("state_machine") or {}).get("states", [])
            for tr in st.get("transitions", [])
        ] + gen.get("transitions", [])
        out["tree_nodes"] = sorted(_node_ids(data["trees"]))
    return out


def _names(value: Any) -> list[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    return [str(v).split("[", 1)[0] for v in items]


def _node_ids(trees: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        found.add(node["node_id"])
        for child in node.get("children", []):
            walk(child)

    for tree in trees:
        walk(tree["root"])
    return found


def build_prompt(state: DraftState, section: str, feedback: str = "") -> str:
    claims: list[dict] = []
    if section in SHOW_CLAIMS:
        claims = compact_evidence(state.package, SECTION_TOPICS[section])
    skeleton = state.skeleton.model_dump(mode="json", exclude_defaults=True)
    if section == "parameters" and state.user_values:
        skeleton = {**skeleton, "user_values": state.user_value_data()}
    return section_prompt(
        section,
        state.request.requirement,
        state.clause_data(),
        skeleton,
        done_sections(state, section),
        catalog_text(section, state.catalog, state.constraints),
        claims,
        state.generated if section not in ("parameters", "structure") else None,
        feedback,
        available_ids(state, section) if section != "parameters" else None,
        state.tactic_block,
    )


def refresh_generated(state: DraftState) -> None:
    if "structure" not in state.sections:
        state.generated = None
        return
    assembled = assemble(state.request, state.skeleton, state.sections, state.package)
    try:
        state.generated = generated_ids(assembled.data, state.catalog)
    except Exception:  # noqa: BLE001 - partial specs may be inconsistent; IDs are a hint
        state.generated = None


async def fill_section(
    state: DraftState, section: str, run: ModelRunner, feedback: str = ""
) -> None:
    schema = SECTION_MODELS[section]
    result = await run(f"section:{section}", build_prompt(state, section, feedback), schema)
    state.sections[section] = result
    if section == "structure":
        refresh_generated(state)


async def fill_all(state: DraftState, run: ModelRunner) -> None:
    for section in SECTIONS:
        await fill_section(state, section, run)
