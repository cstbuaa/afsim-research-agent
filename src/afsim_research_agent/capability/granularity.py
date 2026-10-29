"""Coarse / fine capability judgment (tactical capability groups, ``groups.toml``).

A first draft is judged coarsely: only the core groups of the platform class (for ground
vehicles: maneuver, formation, terrain) are checked interface by interface. Anything else
the platform lacks or has not confirmed is *simplified* (ideal communication, ideal
detection, no damage model, …) instead of becoming a gap, and detail items (fuel,
ammunition count, sensor power, simulation internals) are left out. The user refines a
group when the tactic needs it; ``fine`` judges everything precisely (the AFSIM backend
still requires a decision for every precise gap).
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Literal

from afsim_research_agent.capability.assess import GAP_TYPES, RefResult

Granularity = Literal["coarse", "fine"]
_GROUPS = Path(__file__).with_name("groups.toml")
CONTROL = "control"


@dataclass(frozen=True)
class Group:
    id: str
    display_name: str
    capabilities: frozenset[str]
    detail: frozenset[str]
    detail_interfaces: frozenset[str]
    simplification: str


@dataclass(frozen=True)
class GroupTable:
    version: str
    groups: dict[str, Group]
    core: dict[str, tuple[str, ...]]
    ir_groups: dict[str, str]
    ir_detail: frozenset[str]
    detail_default: str
    by_capability: dict[str, str] = field(default_factory=dict)
    # platform class → IR elements it can never have (e.g. altitude for ground vehicles)
    excluded: dict[str, frozenset[str]] = field(default_factory=dict)

    def not_applicable(self, platform_class: str | None) -> frozenset[str]:
        return self.excluded.get(platform_class or "", frozenset())

    @classmethod
    @cache
    def default(cls) -> GroupTable:
        """Repository groups plus the members kept next to the default capability set."""
        from afsim_research_agent.capability.catalog import (
            GROUP_MEMBERS_FILE,
            default_catalog_root,
        )

        members = default_catalog_root() / GROUP_MEMBERS_FILE
        return cls.from_file(_GROUPS, members if members.exists() else None)

    @classmethod
    def from_file(cls, path: Path, members: Path | None = None) -> GroupTable:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if members is not None:
            extra = tomllib.loads(members.read_text(encoding="utf-8")).get("group", {})
            for gid, lists in extra.items():
                if gid not in data["group"]:
                    raise ValueError(f"{members.name}: unknown group '{gid}'")
                data["group"][gid].update(lists)
        groups = {
            gid: Group(
                id=gid,
                display_name=g["display_name"],
                capabilities=frozenset(g.get("capabilities", ())),
                detail=frozenset(g.get("detail", ())),
                detail_interfaces=frozenset(g.get("detail_interfaces", ())),
                simplification=g.get("simplification", ""),
            )
            for gid, g in data["group"].items()
        }
        by_capability = {}
        for g in groups.values():
            for cap in g.capabilities | g.detail:
                by_capability[cap] = g.id
        ir = data.get("ir", {})
        return cls(
            version=data["meta"]["version"],
            groups=groups,
            core={k: tuple(v["core"]) for k, v in data.get("class", {}).items()},
            ir_groups=dict(ir.get("groups", {})),
            ir_detail=frozenset(ir.get("detail", ())),
            detail_default=data["meta"].get("detail_default", ""),
            by_capability=by_capability,
            excluded={
                k: frozenset(v.get("not_applicable", ())) for k, v in data.get("class", {}).items()
            },
        )

    def group_of(self, ref: str) -> str | None:
        return self.by_capability.get(ref.partition(":")[0])

    def is_detail(self, ref: str) -> bool:
        cap = ref.partition(":")[0]
        group = self.groups.get(self.by_capability.get(cap, ""))
        return group is not None and (cap in group.detail or ref in group.detail_interfaces)

    def core_of(self, platform_class: str | None) -> tuple[str, ...]:
        if platform_class and platform_class in self.core:
            return self.core[platform_class]
        return self.core.get("default", ())


@dataclass(frozen=True)
class CapabilityPolicy:
    """How precisely to judge. ``refine`` lists groups the user wants judged precisely
    (with their detail items) even in a coarse draft."""

    granularity: Granularity = "coarse"
    refine: frozenset[str] = frozenset()
    table: GroupTable = field(default_factory=GroupTable.default, compare=False, repr=False)

    @classmethod
    def make(cls, granularity: str | None = None, refine: Iterable[str] = ()) -> CapabilityPolicy:
        level = granularity or "coarse"
        if level not in ("coarse", "fine"):
            raise ValueError(f"granularity must be coarse or fine, not '{level}'")
        table = GroupTable.default()
        unknown = sorted(set(refine) - set(table.groups))
        if unknown:
            raise ValueError(f"unknown capability groups: {unknown}; known: {sorted(table.groups)}")
        return cls(level, frozenset(refine), table)  # type: ignore[arg-type]

    @property
    def coarse(self) -> bool:
        return self.granularity == "coarse"

    def precise_groups(self, platform_class: str | None) -> set[str]:
        if not self.coarse:
            return set(self.table.groups)
        return set(self.table.core_of(platform_class)) | set(self.refine)

    def simplifies(self, group: str | None, platform_class: str | None, detail: bool) -> bool:
        """Whether a missing or unconfirmed item of ``group`` is simplified, not a gap."""
        if not self.coarse or group in self.refine:
            return False
        return detail or group not in self.table.core_of(platform_class)

    def hides(self, group: str | None, detail: bool) -> bool:
        """Detail items are left out of coarse prompts unless their group is refined."""
        return self.coarse and detail and group not in self.refine

    def simplification(self, group: str | None, detail: bool) -> str:
        g = self.table.groups.get(group or "")
        if detail and (g is None or group in self.table.core_of(None)):
            return self.table.detail_default
        if g is None:
            return self.table.detail_default
        return f"{g.display_name}：{g.simplification}"

    def grade(self, result: RefResult, platform_class: str | None) -> RefResult:
        """Turn a gap into a simplification where the policy allows it."""
        if result.conclusion not in GAP_TYPES:
            return result
        group = self.table.group_of(result.ref)
        detail = self.table.is_detail(result.ref)
        if not self.simplifies(group, platform_class, detail):
            return result
        return replace(result, conclusion="simplified", reason=self.simplification(group, detail))

    def ir_group(self, element: str, refs: Iterable[str] = ()) -> str | None:
        if element in self.table.ir_groups:
            return self.table.ir_groups[element]
        for ref in refs:
            group = self.table.group_of(ref)
            if group:
                return group
        return None

    def ir_detail(self, element: str, refs: Iterable[str] = ()) -> bool:
        if element in self.table.ir_detail:
            return True
        refs = list(refs)
        return bool(refs) and all(self.table.is_detail(r) for r in refs)

    def summary(self) -> dict[str, object]:
        return {"granularity": self.granularity, "refine": sorted(self.refine)}


FINE = CapabilityPolicy("fine")
