"""K4: generation constraints for Falcon from the platform capability catalog.

Integration point ④ (design/capability-catalog-integration-v1.md §3.5). The action,
operand and function lists in Falcon's prompts are filtered by what the target platform
classes can do:

- available (ready / composable / partial): listed normally;
- unknown on the platform (confirm): listed with a "需确认" mark, prefer alternatives;
- not on the platform (develop / not applicable to the platform class): not listed;
- coarse drafts (``granularity.py``): detail items are not listed, and items outside the
  core groups that are missing or unconfirmed are listed plainly (they are simplified).

The constraint is advisory: whatever the model writes is still checked by the IR
capability check (K3) after generation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from afsim_research_agent.capability.assess import (
    LABELS,
    RANK,
    Aliases,
    assess_ir_element,
    map_ir_element,
)
from afsim_research_agent.capability.catalog import CapabilityCatalog, CatalogError, Interface
from afsim_research_agent.capability.granularity import CapabilityPolicy

if TYPE_CHECKING:
    from afsim_research_agent.ir.catalog import Catalog

Tier = Literal["available", "confirm", "unavailable", "hidden"]
_TIER: dict[str, Tier] = {
    "ready": "available",
    "composable": "available",
    "partial": "available",
    "simplified": "available",
    "confirm": "confirm",
    "ir_extension": "confirm",
    "develop": "unavailable",
}


@dataclass
class VocabularyConstraints:
    """IR vocabulary element (action, ``operand.property``, function) → tier."""

    platform_classes: tuple[str, ...]
    catalog_version: str
    tiers: dict[str, Tier] = field(default_factory=dict)
    conclusions: dict[str, str] = field(default_factory=dict)
    # elements only some of the platform classes have → the classes that have them
    only_for: dict[str, tuple[str, ...]] = field(default_factory=dict)
    policy: dict[str, object] | None = None
    # element → catalog entry names, limitations, and how it is simplified (action table)
    details: dict[str, dict[str, str]] = field(default_factory=dict)
    # element → the catalog references it maps to (a need for one of these is in the vocabulary)
    refs: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def tier(self, name: str) -> Tier:
        return self.tiers.get(name, "available")

    def mark(self, name: str) -> str | None:
        """Prompt suffix for an element; None leaves it out of the prompt."""
        tier = self.tier(name)
        if tier in ("unavailable", "hidden"):
            return None
        suffix = "【需确认】" if tier == "confirm" else ""
        if name in self.only_for:
            suffix += f"【仅 {'/'.join(self.only_for[name])}】"
        return suffix

    def label(self, name: str) -> str:
        if self.tier(name) == "hidden":
            return "细节项（按简化处理）"
        conclusion = self.conclusions.get(name)
        return LABELS.get(conclusion, "可用") if conclusion else "可用"

    def allowed(self, name: str, platform_class: str | None = None) -> bool:
        """Whether a role of ``platform_class`` may use the element (closed vocabulary)."""
        if self.tier(name) == "unavailable":
            return False
        only = self.only_for.get(name)
        return not (only and platform_class and platform_class not in only)

    def element_for(self, contract: str) -> str | None:
        """The vocabulary element implementing a catalog reference, if any is listed."""
        for name, refs in self.refs.items():
            if contract in refs and self.tier(name) != "unavailable":
                return name
        return None

    def names(self, tier: Tier) -> list[str]:
        return sorted(n for n, t in self.tiers.items() if t == tier)

    def summary(self) -> dict[str, object]:
        return {
            "platform_classes": list(self.platform_classes),
            "catalog_version": self.catalog_version,
            "confirm": self.names("confirm"),
            "unavailable": self.names("unavailable"),
            "hidden": self.names("hidden"),
            **(self.policy or {}),
        }


def vocabulary_names(ir_catalog: Catalog) -> list[str]:
    names = list(ir_catalog.actions)
    for op in ir_catalog.operands.values():
        names += [f"{op.name}.{prop}" for prop in op.properties]
    names += list(ir_catalog.functions)
    return names


def vocabulary_constraints(
    ir_catalog: Catalog,
    catalog: CapabilityCatalog,
    platform_classes: Iterable[str],
    aliases: Aliases | None = None,
    policy: CapabilityPolicy | None = None,
) -> VocabularyConstraints:
    """Per element, the worst conclusion over the platform classes that have it at all.

    An element no class has is left out; one only some classes have is listed with the
    classes that may use it (roles are not assigned to elements yet when prompts are built)."""
    classes = tuple(sorted(set(platform_classes)))
    result = VocabularyConstraints(
        classes, catalog.content_hash, policy=policy.summary() if policy else None
    )
    for name in vocabulary_names(ir_catalog):
        if policy is not None:
            mapping = map_ir_element(catalog, name, aliases)
            refs = [*mapping.uses, *(r for alt in mapping.any_of for r in alt)]
            group = policy.ir_group(name, refs)
            if policy.hides(group, policy.ir_detail(name, refs)):
                result.tiers[name] = "hidden"
                continue
        judged = {
            cls: assess_ir_element(catalog, name, cls, aliases, policy)
            for cls in classes or (None,)
        }
        per_class = {cls: verdict.conclusion for cls, (_m, verdict) in judged.items()}
        mapping, verdict = next(iter(judged.values()))
        result.details[name] = _details(catalog, mapping, verdict)
        result.refs[name] = tuple(
            dict.fromkeys([*mapping.uses, *(r for alt in mapping.any_of for r in alt)])
        )
        having = {c: v for c, v in per_class.items() if v != "develop"}
        worst = max((having or per_class).values(), key=RANK.__getitem__)
        result.conclusions[name] = worst
        result.tiers[name] = _TIER[worst]
        if having and len(having) < len(per_class):
            result.only_for[name] = tuple(c for c in having if c)
    return result


def _details(catalog: CapabilityCatalog, mapping: object, verdict: object) -> dict[str, str]:
    """Catalog entry names and the first limitation, for the generation prompt."""
    refs = list(getattr(mapping, "uses", ())) + [
        r for alt in getattr(mapping, "any_of", ()) for r in alt
    ]
    names: list[str] = []
    limits: list[str] = []
    for ref in refs[:3]:
        try:
            entry = catalog.resolve(ref)
        except CatalogError:
            continue
        if isinstance(entry, Interface):
            names.append(entry.display_name)
            cap = catalog.get(entry.capability_id)
            if entry.note:
                limits.append(entry.note)
        else:
            names.append(entry.display_name)
            cap = entry
        if cap is not None and cap.limitations:
            limits.append(cap.limitations[0])
    out = {"catalog": "、".join(dict.fromkeys(names))}
    if limits:
        out["limits"] = limits[0][:80]
    reasons = getattr(verdict, "reasons", None) or []
    if getattr(verdict, "conclusion", "") == "simplified" and reasons:
        out["simplified"] = str(reasons[0])[:60]
    return {k: v for k, v in out.items() if v}


CONSTRAINT_NOTE = (
    "平台能力约束（由能力目录生成）：只使用下面列出的动作、操作数与函数；"
    "标【需确认】的平台支持尚未确认，有等效写法时优先用其他条目，确需使用时照常写，"
    "生成后会列为待确认缺口。未列出的条目平台不具备，不要使用（编译时会拒绝）；"
    "需要表中没有的能力时，在 ```yaml needs``` 中登记，树里用替代写法。"
)
