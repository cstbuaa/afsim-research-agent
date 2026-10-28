"""Read-only access to the platform capability catalog (formal layer only).

Layout (design/capability-catalog-integration-v1.md §3.1)::

    <root>/capability_catalog.yaml   0.2.0 single file (domains not yet split)
    <root>/catalog/index.yaml        retrieval index over all domains
    <root>/catalog/<domain>.yaml     0.3.0 part files
    <root>/capability_overrides.yaml write-back of resolved gaps (K5): user confirmations
                                     and plugin implementations, applied last

Capabilities in a part file replace the 0.2.0 entry with the same ID. The local layer
(``_local/``, ``*.local.*``: native names, bindings, manual pages) is never read here, and a
catalog that is not ``classification: public`` is refused unless explicitly allowed, because
this module feeds prompts sent to cloud models.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

PUBLIC = "public"
OVERRIDES_FILE = "capability_overrides.yaml"
# status a resolved reference gets: interfaces vs capabilities use different vocabularies
_RESOLVED = {"interface": "supported", "capability": "available"}
_REPO_DEFAULT = (
    Path(__file__).resolve().parents[3] / "datasets" / "platform_capabilities" / "afsim-2.9"
)


class CatalogError(ValueError):
    """The catalog cannot be loaded or a reference does not resolve."""


@dataclass(frozen=True)
class Interface:
    capability_id: str
    direction: Literal["accepts", "provides"]
    ir: str
    display_name: str
    status: str
    ir_v1: str | None
    basis: str | None = None
    note: str | None = None
    ir_v1_declared: bool = True  # False: the catalog has not recorded the IR mapping yet

    @property
    def ref(self) -> str:
        return f"{self.capability_id}:{self.ir}"


@dataclass(frozen=True)
class Capability:
    id: str
    domain: str
    display_name: str
    status: str
    kind: str = ""
    subcategory: str | None = None
    summary: str = ""
    keywords: tuple[str, ...] = ()
    tactical_uses: tuple[str, ...] = ()
    platform_classes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    composed_of: tuple[str, ...] = ()
    ir_v1: tuple[str, ...] = ()
    interfaces: tuple[Interface, ...] = ()
    limitations: tuple[str, ...] = ()
    basis: str | None = None
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def interface(self, ir: str) -> Interface | None:
        return next((i for i in self.interfaces if i.ir == ir), None)

    @property
    def is_composite(self) -> bool:
        return self.kind == "composite"


def default_catalog_root() -> Path:
    """``CROW_CAPABILITY_CATALOG`` if set, otherwise the copy kept in the repository."""
    configured = os.getenv("CROW_CAPABILITY_CATALOG")
    return Path(configured).expanduser() if configured else _REPO_DEFAULT


def _guard(path: Path) -> None:
    if "_local" in path.parts or ".local." in path.name:
        raise CatalogError(f"refusing to read local-only file: {path.name}")


def _read_yaml(path: Path) -> dict[str, Any]:
    _guard(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read {path.name}: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name} is not a mapping")
    return data


def _capability(raw: dict[str, Any], source: str) -> Capability:
    interfaces: list[Interface] = []
    for direction in ("accepts", "provides"):
        for item in raw.get(direction) or []:
            interfaces.append(
                Interface(
                    capability_id=raw["id"],
                    direction=direction,  # type: ignore[arg-type]
                    ir=item["ir"],
                    display_name=item.get("display_name", item["ir"]),
                    status=str(item.get("status", "unknown")),
                    ir_v1=item.get("ir_v1"),
                    basis=item.get("basis"),
                    note=item.get("note"),
                    ir_v1_declared="ir_v1" in item,
                )
            )
    nl = raw.get("natural_language") or {}
    composed = tuple(
        c["capability"] if isinstance(c, dict) else str(c) for c in raw.get("composed_of") or []
    )
    confirmation = raw.get("confirmation") or {}
    return Capability(
        id=raw["id"],
        domain=raw.get("domain", ""),
        display_name=raw.get("display_name", raw["id"]),
        status=str(raw.get("status", "unknown")),
        kind=raw.get("kind", ""),
        subcategory=raw.get("subcategory"),
        summary=nl.get("definition") or raw.get("description", ""),
        keywords=tuple(raw.get("keywords") or ()),
        tactical_uses=tuple(raw.get("tactical_uses") or ()),
        platform_classes=tuple(raw.get("applicable_platform_classes") or ()),
        dependencies=tuple(raw.get("dependencies") or ()),
        composed_of=composed,
        ir_v1=tuple(raw.get("ir_v1") or ()),
        interfaces=tuple(interfaces),
        limitations=tuple(raw.get("limitations") or ()),
        basis=confirmation.get("basis"),
        source=source,
        raw=raw,
    )


def _apply_override(caps: dict[str, Capability], item: dict[str, Any]) -> None:
    """Mark a reference as resolved (written back by the gap registry, K5)."""
    ref = str(item.get("ref", ""))
    cap_id, _, ir = ref.partition(":")
    cap = caps.get(cap_id)
    if cap is None:
        raise CatalogError(f"{OVERRIDES_FILE}: unknown capability '{cap_id}'")
    basis = item.get("basis")
    if not ir:
        status = str(item.get("status", _RESOLVED["capability"]))
        caps[cap_id] = replace(cap, status=status, basis=basis or cap.basis)
        return
    if cap.interface(ir) is None:
        raise CatalogError(f"{OVERRIDES_FILE}: capability '{cap_id}' has no interface '{ir}'")
    status = str(item.get("status", _RESOLVED["interface"]))
    interfaces = tuple(
        replace(i, status=status, basis=basis or i.basis) if i.ir == ir else i
        for i in cap.interfaces
    )
    # a capability whose status was unknown becomes partly available once an interface is
    cap_status = "partial" if cap.status in ("unknown", "unavailable") else cap.status
    caps[cap_id] = replace(cap, interfaces=interfaces, status=cap_status)


class CapabilityCatalog:
    """All formal-layer capabilities of one platform, with lookups by ID, interface and IR name."""

    def __init__(
        self,
        capabilities: Iterable[Capability],
        *,
        catalog_id: str,
        platform: str,
        content_hash: str,
        domains: dict[str, str] | None = None,
        overrides: list[dict[str, Any]] | None = None,
    ) -> None:
        self.capabilities: dict[str, Capability] = {}
        for cap in capabilities:
            self.capabilities[cap.id] = cap
        self.catalog_id = catalog_id
        self.platform = platform
        self.content_hash = content_hash
        self.domains = domains or {}
        self.overrides = list(overrides or [])
        self._by_ir_v1: dict[str, list[Interface | Capability]] = {}
        for cap in self.capabilities.values():
            for name in cap.ir_v1:
                self._by_ir_v1.setdefault(name, []).append(cap)
            for iface in cap.interfaces:
                if iface.ir_v1:
                    self._by_ir_v1.setdefault(iface.ir_v1, []).append(iface)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(
        cls, root: Path | str | None = None, *, allow_private: bool = False
    ) -> CapabilityCatalog:
        root = Path(root) if root is not None else default_catalog_root()
        single = root / "capability_catalog.yaml"
        parts_dir = root / "catalog"
        if not single.exists() and not parts_dir.is_dir():
            raise CatalogError(f"no capability catalog under {root}")
        files: list[Path] = []
        caps: dict[str, Capability] = {}
        meta: dict[str, Any] = {}
        domains: dict[str, str] = {}
        if single.exists():
            data = _read_yaml(single)
            files.append(single)
            meta = data.get("catalog") or {}
            cls._check_classification(meta, single.name, allow_private)
            for raw in data.get("capabilities") or []:
                caps[raw["id"]] = _capability(raw, single.name)
            for dom in data.get("capability_domains") or []:
                domains.setdefault(dom["id"], dom.get("display_name", dom["id"]))
        if parts_dir.is_dir():
            for path in sorted(parts_dir.glob("*.yaml")):
                if path.name == "index.yaml":
                    continue
                data = _read_yaml(path)
                files.append(path)
                part_meta = data.get("catalog") or {}
                cls._check_classification(part_meta, path.name, allow_private)
                meta = meta or part_meta
                part = data.get("part") or {}
                if part.get("domain"):
                    domains[part["domain"]] = part.get("display_name", part["domain"])
                for raw in data.get("capabilities") or []:
                    caps[raw["id"]] = _capability(raw, f"catalog/{path.name}")
        overrides: list[dict[str, Any]] = []
        override_path = root / OVERRIDES_FILE
        if override_path.exists():
            data = _read_yaml(override_path)
            files.append(override_path)
            overrides = [o for o in data.get("overrides") or [] if isinstance(o, dict)]
            for item in overrides:
                _apply_override(caps, item)
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return cls(
            caps.values(),
            catalog_id=str(meta.get("id", root.name)),
            platform=" ".join(
                str(meta[k]) for k in ("platform_family", "platform_version") if meta.get(k)
            ),
            content_hash=f"sha256:{digest.hexdigest()}",
            domains=domains,
            overrides=overrides,
        )

    @staticmethod
    def _check_classification(meta: dict[str, Any], name: str, allow_private: bool) -> None:
        level = meta.get("classification", PUBLIC)
        if level != PUBLIC and not allow_private:
            raise CatalogError(
                f"{name} is classified '{level}'; only the public formal layer may be loaded"
            )

    # ------------------------------------------------------------------ lookups
    def get(self, capability_id: str) -> Capability | None:
        return self.capabilities.get(capability_id)

    def resolve(self, ref: str) -> Capability | Interface:
        """``capability_id`` or ``capability_id:interface_ir`` → the catalog object."""
        cap_id, _, ir = ref.partition(":")
        cap = self.capabilities.get(cap_id)
        if cap is None:
            raise CatalogError(f"unknown capability '{cap_id}'")
        if not ir:
            return cap
        iface = cap.interface(ir)
        if iface is None:
            raise CatalogError(f"capability '{cap_id}' has no interface '{ir}'")
        return iface

    def by_ir_v1(self, name: str) -> list[Interface | Capability]:
        """Catalog entries that implement an IR 1.0 action, operand property, function or
        ``blackboard``. Interfaces come before capability-level links."""
        found = self._by_ir_v1.get(name, [])
        return sorted(found, key=lambda x: 0 if isinstance(x, Interface) else 1)

    def for_platform(self, platform_class: str) -> list[Capability]:
        return [
            c
            for c in self.capabilities.values()
            if not c.platform_classes or platform_class in c.platform_classes
        ]

    def search(
        self,
        text: str,
        *,
        domains: Iterable[str] | None = None,
        platform_class: str | None = None,
        limit: int = 10,
    ) -> list[Capability]:
        """Rank capabilities by overlap of their keywords, tactical uses and names with ``text``.

        Phrases match in either direction (a keyword inside the text, or the text inside a
        keyword), which works for Chinese without word segmentation."""
        wanted = set(domains) if domains is not None else None
        query = text.lower()
        scored: list[tuple[float, str, Capability]] = []
        for cap in self.capabilities.values():
            if wanted is not None and cap.domain not in wanted:
                continue
            if (
                platform_class
                and cap.platform_classes
                and platform_class not in cap.platform_classes
            ):
                continue
            score = 0.0
            for weight, phrases in (
                (3.0, cap.tactical_uses),
                (2.0, cap.keywords),
                (2.0, (cap.display_name,)),
            ):
                for phrase in phrases:
                    p = phrase.lower()
                    if p and (p in query or (len(query) >= 2 and query in p)):
                        score += weight
            if score:
                scored.append((score, cap.id, cap))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [cap for _, _, cap in scored[:limit]]

    def __len__(self) -> int:
        return len(self.capabilities)

    def __contains__(self, capability_id: object) -> bool:
        return capability_id in self.capabilities
