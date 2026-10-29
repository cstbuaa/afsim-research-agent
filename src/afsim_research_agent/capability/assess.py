"""Judgment engine: catalog status → conclusion and gap type.

Shared by the Crow capability annotation (C4b) and the IR capability check
(design/capability-catalog-integration-v1.md §3.3, crow-tactic-proposal-v1.md §18).
Status comes from the catalog only; callers say *what* is needed, never whether it works.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from afsim_research_agent.capability.catalog import (
    CapabilityCatalog,
    CatalogError,
    Interface,
)

Conclusion = Literal[
    "ready", "composable", "partial", "simplified", "confirm", "ir_extension", "develop"
]
Necessity = Literal["required", "enhancing", "substitutable"]

# Best first. A combination of several needs takes the worst; alternatives take the best.
# "simplified": missing or unconfirmed, but outside the groups a coarse draft checks
# precisely; handled by a stated simplification instead of a gap (granularity.py).
RANK: dict[str, int] = {
    "ready": 0,
    "composable": 1,
    "partial": 2,
    "simplified": 3,
    "confirm": 4,
    "ir_extension": 5,
    "develop": 6,
}
GAP_TYPES: dict[str, str] = {
    "confirm": "confirm",
    "ir_extension": "ir_extension",
    "develop": "develop",
}
LABELS: dict[str, str] = {
    "ready": "可实现",
    "composable": "可组合实现",
    "partial": "部分可用",
    "simplified": "简化处理",
    "confirm": "需确认",
    "ir_extension": "需扩展 IR",
    "develop": "需开发",
}
_INTERFACE_STATUS = {
    "supported": "ready",
    "available": "ready",
    "partial": "partial",
    "unknown": "confirm",
    "ir_gap": "ir_extension",
    "unavailable": "develop",
    "platform_missing": "develop",
}
_CAPABILITY_STATUS = {
    "available": "ready",
    "partial": "partial",
    "unknown": "confirm",
    "unavailable": "develop",
}

if TYPE_CHECKING:
    from afsim_research_agent.capability.granularity import CapabilityPolicy


def worst(conclusions: Iterable[str]) -> str:
    items = list(conclusions)
    return max(items, key=RANK.__getitem__) if items else "ready"


def best(conclusions: Iterable[str]) -> str:
    items = list(conclusions)
    return min(items, key=RANK.__getitem__) if items else "develop"


def gap_id(key: str) -> str:
    """Stable gap number from the contract (or the normalised need when nothing maps)."""
    norm = " ".join(key.split()).lower()
    return "cap-gap-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class RefResult:
    ref: str
    conclusion: str
    status: str
    display_name: str
    basis: str | None = None
    reason: str | None = None


@dataclass
class Verdict:
    """Outcome for one need or one IR element."""

    conclusion: str
    refs: list[RefResult] = field(default_factory=list)
    alternatives: list[list[RefResult]] = field(default_factory=list)
    chosen_alternative: int | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def gap_type(self) -> str | None:
        return GAP_TYPES.get(self.conclusion)

    @property
    def label(self) -> str:
        return LABELS[self.conclusion]

    def blocking_refs(self) -> list[RefResult]:
        """The refs that make this verdict a gap (from the uses and the chosen alternative)."""
        pool = list(self.refs)
        if self.chosen_alternative is not None:
            pool += self.alternatives[self.chosen_alternative]
        return [r for r in pool if r.conclusion in GAP_TYPES]


# ---------------------------------------------------------------------- single references
def ir_expressible(
    catalog: CapabilityCatalog, iface: Interface, aliases: Aliases | None = None
) -> bool:
    """Whether IR 1.0 can use this interface: directly (``ir_v1``), through a capability-level
    link, or as part of an aliased IR element (e.g. track position inside
    ``nearest_hostile_range``). Interfaces whose mapping was never recorded (0.2.0 entries)
    count as expressible: an unrecorded mapping is not evidence of an IR gap."""
    if iface.ir_v1 or not iface.ir_v1_declared:
        return True
    cap = catalog.get(iface.capability_id)
    if cap is not None and cap.ir_v1:
        return True
    reachable = (aliases or Aliases.default()).reachable_refs()
    return iface.ref in reachable or iface.capability_id in reachable


def assess_ref(
    catalog: CapabilityCatalog,
    ref: str,
    platform_class: str | None = None,
    *,
    need_ir: bool = False,
) -> RefResult:
    """Status of one catalog reference for an optional platform class.

    ``need_ir``: the caller must express the interface in IR (Crow needs); an interface IR 1.0
    cannot reach in any way then becomes ``ir_extension`` unless the platform side is missing.
    """
    try:
        target = catalog.resolve(ref)
    except CatalogError as exc:
        return RefResult(ref, "develop", "missing", ref, reason=str(exc))
    cap = catalog.get(target.capability_id if isinstance(target, Interface) else target.id)
    assert cap is not None
    if platform_class and cap.platform_classes and platform_class not in cap.platform_classes:
        return RefResult(
            ref,
            "develop",
            "not_applicable",
            cap.display_name,
            reason=f"'{cap.display_name}' 不适用于 {platform_class}",
        )
    if isinstance(target, Interface):
        conclusion = _INTERFACE_STATUS.get(target.status, "confirm")
        if need_ir and conclusion != "develop" and not ir_expressible(catalog, target):
            conclusion = "ir_extension"
        # an interface is never better than its capability says it is
        cap_conclusion = _CAPABILITY_STATUS.get(cap.status, "confirm")
        if cap_conclusion == "develop":
            conclusion = "develop"
        return RefResult(
            ref,
            conclusion,
            target.status,
            f"{cap.display_name} · {target.display_name}",
            basis=target.basis or cap.basis,
        )
    conclusion = _CAPABILITY_STATUS.get(cap.status, "confirm")
    if cap.is_composite and conclusion == "ready":
        conclusion = "composable"
    if (
        need_ir
        and conclusion != "develop"
        and cap.kind != "slice"
        and not cap.ir_v1
        and cap.id not in (Aliases.default().reachable_refs())
    ):
        # implemented on the platform, but no IR 1.0 element can use it
        conclusion = "ir_extension"
    return RefResult(ref, conclusion, cap.status, cap.display_name, basis=cap.basis)


def assess(
    catalog: CapabilityCatalog,
    uses: Sequence[str] = (),
    any_of: Sequence[Sequence[str]] = (),
    platform_class: str | None = None,
    *,
    need_ir: bool = False,
    policy: CapabilityPolicy | None = None,
) -> Verdict:
    """All of ``uses`` are needed; of ``any_of`` the best alternative is taken.

    With a ``policy``, gaps outside the precisely checked groups become ``simplified``."""

    def one(ref: str) -> RefResult:
        result = assess_ref(catalog, ref, platform_class, need_ir=need_ir)
        return policy.grade(result, platform_class) if policy is not None else result

    refs = [one(r) for r in uses]
    alternatives = [[one(r) for r in alt] for alt in any_of]
    parts = [r.conclusion for r in refs]
    chosen = None
    if alternatives:
        scores = [worst(r.conclusion for r in alt) for alt in alternatives]
        chosen = min(range(len(scores)), key=lambda i: (RANK[scores[i]], i))
        parts.append(scores[chosen])
    if not parts:
        return Verdict("develop", reasons=["没有对应的目录条目"])
    conclusion = worst(parts)
    # several ready parts combined are a composition, not a single native interface
    if conclusion == "ready" and len(refs) + (1 if alternatives else 0) > 1:
        conclusion = "composable"
    reasons = [r.reason for r in refs if r.reason]
    if chosen is not None:
        reasons += [
            r.reason for r in alternatives[chosen] if r.reason and r.conclusion == "simplified"
        ]
    return Verdict(conclusion, refs, alternatives, chosen, list(dict.fromkeys(reasons)))


# ---------------------------------------------------------------------- IR vocabulary
@dataclass(frozen=True)
class Aliases:
    version: str
    bindings: dict[str, tuple[str, ...]]
    elements: dict[str, dict]
    static_bindings: frozenset[str] = frozenset()

    @classmethod
    @cache
    def default(cls) -> Aliases:
        """``ir_alignment.toml`` next to the default capability set; empty without one."""
        from afsim_research_agent.capability.catalog import ALIGNMENT_FILE, default_catalog_root

        path = default_catalog_root() / ALIGNMENT_FILE
        if not path.exists():
            return cls(version="none", bindings={}, elements={})
        return cls.from_file(path)

    def reachable_refs(self) -> frozenset[str]:
        """Every catalog reference some IR element or role dependency is mapped to."""
        refs: set[str] = {r for uses in self.bindings.values() for r in uses}
        for entry in self.elements.values():
            refs.update(entry.get("uses", ()))
            refs.update(r for alt in entry.get("any_of", ()) for r in alt)
        return frozenset(refs)

    @classmethod
    def from_file(cls, path: Path) -> Aliases:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=data["meta"]["version"],
            bindings={k: tuple(v.get("uses", ())) for k, v in data.get("binding", {}).items()},
            elements=dict(data.get("element", {})),
            static_bindings=frozenset(
                k for k, v in data.get("binding", {}).items() if v.get("static")
            ),
        )


@dataclass(frozen=True)
class ElementMapping:
    """How an IR element is obtained on the platform."""

    name: str
    kind: Literal["direct", "alias", "static", "unmapped"]
    uses: tuple[str, ...] = ()
    any_of: tuple[tuple[str, ...], ...] = ()
    note: str | None = None


def map_ir_element(
    catalog: CapabilityCatalog, name: str, aliases: Aliases | None = None
) -> ElementMapping:
    """IR action, ``operand.property``, function or ``blackboard`` → catalog references.

    Direct ``ir_v1`` links win. Several interfaces implementing the same element are
    alternatives (e.g. typed, addressed and group messages all implement ``send_message``)."""
    aliases = aliases or Aliases.default()
    entry = aliases.elements.get(name)
    if entry is not None and entry.get("override"):
        return _from_alias(name, entry)
    direct = catalog.by_ir_v1(name)
    interfaces = [x for x in direct if isinstance(x, Interface)]
    if interfaces:
        refs = tuple(dict.fromkeys(i.ref for i in interfaces))
        if len(refs) == 1:
            return ElementMapping(name, "direct", uses=refs)
        return ElementMapping(name, "direct", any_of=tuple((r,) for r in refs))
    if entry is not None:
        return _from_alias(name, entry)
    if direct:  # capability-level links only; several capabilities are alternatives
        ids = tuple(dict.fromkeys(c.id for c in direct))
        if len(ids) == 1:
            return ElementMapping(name, "direct", uses=ids)
        return ElementMapping(name, "direct", any_of=tuple((i,) for i in ids))
    return ElementMapping(name, "unmapped")


def _from_alias(name: str, entry: dict) -> ElementMapping:
    if entry.get("static"):
        return ElementMapping(name, "static", note=entry.get("note"))
    return ElementMapping(
        name,
        "alias",
        uses=tuple(entry.get("uses", ())),
        any_of=tuple(tuple(a) for a in entry.get("any_of", ())),
        note=entry.get("note"),
    )


def assess_ir_element(
    catalog: CapabilityCatalog,
    name: str,
    platform_class: str | None = None,
    aliases: Aliases | None = None,
    policy: CapabilityPolicy | None = None,
) -> tuple[ElementMapping, Verdict]:
    mapping = map_ir_element(catalog, name, aliases)
    if mapping.kind == "static":
        return mapping, Verdict("ready", reasons=[mapping.note or "由模型定义确定"])
    if mapping.kind == "unmapped":
        if policy is not None:
            group = policy.ir_group(name)
            detail = policy.ir_detail(name)
            if policy.simplifies(group, platform_class, detail):
                return mapping, Verdict(
                    "simplified", reasons=[policy.simplification(group, detail)]
                )
        return mapping, Verdict("develop", reasons=["能力目录中没有对应条目"])
    verdict = assess(catalog, mapping.uses, mapping.any_of, platform_class, policy=policy)
    if mapping.note:
        verdict.reasons.append(mapping.note)
    return mapping, verdict


def map_binding(capability: str, aliases: Aliases | None = None) -> tuple[str, ...]:
    """IR role-dependency capability ID (e.g. ``voice_data``) → catalog capability IDs."""
    aliases = aliases or Aliases.default()
    return aliases.bindings.get(capability, ())


# ---------------------------------------------------------------------- feasibility summary
@dataclass(frozen=True)
class Feasibility:
    level: Literal["ready", "needs_confirmation", "needs_development"]
    counts: dict[str, int]
    blocking: tuple[str, ...]


def summarize(
    items: Iterable[tuple[Necessity, Verdict, str | Sequence[str]]],
    *,
    development: Sequence[str] = ("develop", "ir_extension"),
) -> Feasibility:
    """``(necessity, verdict, gap ids)`` per need → feasibility of the whole tactic or model.

    Only required and substitutable needs block (substitutable needs are already resolved to
    their best alternative inside the verdict); enhancing needs are counted but never block.
    ``development`` lists the conclusions that make the level ``needs_development``; Crow
    passes only ``develop`` because IR expressibility is decided precisely at the IR check."""
    counts = {k: 0 for k in RANK}
    blocking: list[str] = []
    level = "ready"
    for necessity, verdict, gids in items:
        counts[verdict.conclusion] += 1
        if necessity == "enhancing" or verdict.gap_type is None:
            continue
        blocking += [gids] if isinstance(gids, str) else list(gids)
        if verdict.conclusion in development:
            level = "needs_development"
        elif level == "ready":
            level = "needs_confirmation"
    return Feasibility(level, counts, tuple(dict.fromkeys(blocking)))  # type: ignore[arg-type]
