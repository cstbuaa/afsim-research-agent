"""The exported capability set seen through the orchestrator's extension interfaces.

``CapabilitySetActionLibrary`` (动作集) lists the actions and tactic steps of every slice;
``CapabilitySetTacticLibrary`` (战术集) lists tactic subtrees and mission state machines,
with the actions each one relies on. Both read the same public export as the capability
check, so what the agent is offered and what the check accepts cannot drift apart.

They are the built-in defaults: ``setup_defaults`` fills only empty slots, so an
implementation registered through ``AFSIM_AGENT_EXTENSIONS`` or an entry point wins.
"""

from __future__ import annotations

import re
from typing import Any

from afsim_research_agent.capability.catalog import Capability, CapabilityCatalog, CatalogError
from afsim_research_agent.orchestrator.extensions import (
    ActionSpec,
    ExtensionRegistry,
    TacticSpec,
)

_ACTION_KINDS = ("action", "tactic_step")


def _param_text(spec: dict[str, Any]) -> str:
    text = str(spec.get("type", "value"))
    unit = spec.get("units") or spec.get("unit")
    if unit:
        text += f" [{unit}]"
    if spec.get("required"):
        text += "（必填）"
    return text


class CapabilitySetActionLibrary:
    name = "capability-set-actions"

    def __init__(self, catalog: CapabilityCatalog | None = None) -> None:
        self.catalog = catalog or CapabilityCatalog.load()
        self.version = self.catalog.content_hash[:19]

    def _spec(self, cap: Capability) -> ActionSpec:
        raw = cap.raw
        params: dict[str, str] = {}
        for name, p in (raw.get("parameters") or {}).items():
            params[name] = _param_text(p or {})
        for item in raw.get("inputs") or []:
            params.setdefault(item["name"], _param_text(item))
        effects = list(raw.get("effects") or [])
        effects += [f"限制：{x}" for x in cap.limitations]
        return ActionSpec(
            name=cap.id,
            description=f"{cap.display_name}：{cap.summary}" if cap.summary else cap.display_name,
            platform_classes=list(cap.platform_classes),
            parameters=params,
            preconditions=list(raw.get("preconditions") or []),
            effects=effects,
            category=f"{cap.domain}/{cap.kind}",
            source=f"capability set {self.catalog.catalog_id}",
        )

    def actions(self, platform_class: str | None = None) -> list[ActionSpec]:
        out = []
        for cap in self.catalog.capabilities.values():
            if cap.kind not in _ACTION_KINDS:
                continue
            if (
                platform_class
                and cap.platform_classes
                and platform_class not in cap.platform_classes
            ):
                continue
            out.append(self._spec(cap))
        return out

    def get(self, name: str) -> ActionSpec | None:
        cap = self.catalog.get(name)
        return self._spec(cap) if cap is not None and cap.kind != "slice" else None


class CapabilitySetTacticLibrary:
    name = "capability-set-tactics"

    def __init__(self, catalog: CapabilityCatalog | None = None) -> None:
        self.catalog = catalog or CapabilityCatalog.load()
        self.version = self.catalog.content_hash[:19]
        self._subtrees: dict[str, dict[str, Any]] = {}
        self._state_trees: dict[str, dict[str, Any]] = {}
        self._items: dict[str, TacticSpec] = {}
        for data in self.catalog.tactics.values():
            for st in data.get("subtrees") or []:
                self._subtrees[st["id"]] = st
            for st in data.get("state_trees") or []:
                self._state_trees[st["id"]] = st
        for sid, data in self.catalog.tactics.items():
            for st in data.get("subtrees") or []:
                self._items[st["id"]] = self._subtree_spec(sid, st)
            for m in data.get("missions") or []:
                self._items[m["id"]] = self._mission_spec(sid, m)

    # ------------------------------------------------------------------ closure
    def _closure(self, tree_ids: list[str], seen: set[str] | None = None) -> list[str]:
        seen = seen if seen is not None else set()
        actions: list[str] = []
        for tid in tree_ids:
            if tid in seen:
                continue
            seen.add(tid)
            node = self._subtrees.get(tid) or self._state_trees.get(tid)
            if node is None:
                continue
            uses = node.get("uses") or {}
            actions += uses.get("actions", [])
            nested = list(uses.get("subtrees", [])) + list((node.get("states") or {}).values())
            actions += self._closure(nested, seen)
        return list(dict.fromkeys(actions))

    def _label(self, action_id: str) -> str:
        cap = self.catalog.get(action_id)
        return cap.display_name if cap else action_id

    def _subtree_spec(self, sid: str, st: dict[str, Any]) -> TacticSpec:
        actions = self._closure([st["id"]])
        desc = st.get("description") or "、".join(self._label(a) for a in actions)
        return TacticSpec(
            name=st["id"],
            description=f"战术子树：{desc}",
            actions=actions,
            applicability=self.catalog.domains.get(sid, sid),
            source=f"capability set {self.catalog.catalog_id}",
        )

    def _mission_spec(self, sid: str, m: dict[str, Any]) -> TacticSpec:
        trees = m.get("state_trees") or {}
        roles = sorted(
            {
                st.get("role")
                for st in self._state_trees.values()
                if st["id"].startswith(f"{sid}.") and st.get("role")
            }
        )
        phases = []
        for state in m.get("states") or []:
            leaving = [
                f"{t.get('when')} → {t['to']}"
                for t in m.get("transitions") or []
                if t.get("from") == state
            ]
            phase: dict[str, Any] = {"phase": state}
            if trees.get(state):
                phase["tree"] = trees[state]
            if leaving:
                phase["until"] = leaving
            phases.append(phase)
        tree_ids = list(trees.values()) or [
            st["id"] for st in self._state_trees.values() if st["id"].startswith(f"{sid}.")
        ]
        return TacticSpec(
            name=m["id"],
            description=m.get("description")
            or f"任务状态机（{self.catalog.domains.get(sid, sid)}）",
            phases=phases,
            roles=roles,
            actions=self._closure(tree_ids),
            parameters={k: str(v) for k, v in (m.get("parameters") or {}).items()},
            applicability=self.catalog.domains.get(sid, sid),
            source=f"capability set {self.catalog.catalog_id}",
        )

    # ------------------------------------------------------------------ interface
    def tactics(self) -> list[TacticSpec]:
        return list(self._items.values())

    def get(self, name: str) -> TacticSpec | None:
        return self._items.get(name)

    def match(self, query: str, *, limit: int = 5) -> list[TacticSpec]:
        """Rank by overlap of the query with names, descriptions, slice names and the
        display names / tactical uses of the actions a tactic relies on (works for Chinese
        without segmentation: any phrase found inside the query counts)."""
        q = query.lower()
        words = {w for w in re.split(r"[\s_.,，、。]+", q) if len(w) >= 2}
        scored = []
        for spec in self._items.values():
            phrases = [spec.name, spec.description, spec.applicability]
            for a in spec.actions:
                cap = self.catalog.get(a)
                if cap:
                    phrases += [cap.display_name, *cap.tactical_uses, *cap.keywords]
            score = 0.0
            for p in phrases:
                p = (p or "").lower()
                if not p:
                    continue
                if len(p) >= 2 and p in q:
                    score += 2.0
                score += sum(1.0 for w in words if w in p)
            if spec.phases:
                score += 0.5  # whole missions first when equally relevant
            if score:
                scored.append((score, spec.name, spec))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [s for _, _, s in scored[:limit]]


def setup_defaults(registry: ExtensionRegistry) -> None:
    """Fill empty action / tactic slots from the capability set (if it can be loaded)."""
    if registry.action_library is not None and registry.tactic_library is not None:
        return
    try:
        catalog = CapabilityCatalog.load()
    except CatalogError as exc:
        registry.load_errors.append(f"capability set: {exc}")
        return
    if registry.action_library is None:
        registry.action_library = CapabilitySetActionLibrary(catalog)
    if registry.tactic_library is None:
        registry.tactic_library = CapabilitySetTacticLibrary(catalog)
    registry.loaded_from.append("builtin:capability_set")
