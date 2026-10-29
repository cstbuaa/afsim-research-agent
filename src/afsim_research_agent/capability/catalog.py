"""Read-only access to the platform capability set (formal layer only).

The capability set is exported from the vehicle lab project (``lab_export.py``)::

    <root>/index.yaml                 catalog metadata and slices
    <root>/action_set/<slice>.yaml    primitives, actions, tactic steps (动作集)
    <root>/tactic_set/<slice>.yaml    tactic subtrees, state trees, missions (战术集)
    <root>/capability_overrides.yaml  write-back of resolved gaps (K5): user confirmations
                                      and plugin implementations, applied last

The whole set is local only (it is derived from the lab project and never committed);
``CROW_CAPABILITY_CATALOG`` or the default path below points to it.

Every slice and every action-set entry becomes one ``Capability``. Names, IR links and
retrieval phrases come from the agent-side ``<root>/ir_alignment.toml``; status comes from the
export (``implemented`` → available; tactic steps without their own contract → partial).
Anything not in the set is not implemented. The local layer (``_local/``, ``*.local.*``)
is never read, and a set that is not ``classification: public`` is refused unless
explicitly allowed, because this module feeds prompts sent to cloud models.
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
    Path(__file__).resolve().parents[3]
    / "datasets"
    / "platform_capabilities"
    / "armored-vehicle-afsim-2.9"
)
# agent-side files kept next to the (local) capability set
ALIGNMENT_FILE = "ir_alignment.toml"
GROUP_MEMBERS_FILE = "group_members.toml"
SET_FORMAT = "CapabilitySet"
# export status → catalog status used by the judgment engine
_SET_STATUS = {"implemented": "available", "partial": "partial", "unavailable": "unavailable"}


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


def _alignment(path: Path) -> dict[str, Any]:
    import tomllib

    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("entry", {})


def _entry_capability(
    raw: dict[str, Any],
    slice_id: str,
    source: str,
    platform_classes: tuple[str, ...],
    notes: dict[str, Any],
) -> Capability:
    kind = str(raw.get("kind", "action"))
    status = _SET_STATUS.get(str(raw.get("status", "implemented")), "unknown")
    limitations = [str(x) for x in raw.get("limitations") or []]
    if kind == "tactic_step":
        status = "partial"
        users = "、".join(raw.get("used_by") or [slice_id])
        limitations.insert(0, f"只在 {users} 战术内部实现，没有独立动作契约")
    if notes.get("note"):
        limitations.append(str(notes["note"]))
    return Capability(
        id=raw["id"],
        domain=slice_id,
        display_name=str(notes.get("display_name") or raw["id"]),
        status=status,
        kind=kind,
        subcategory=kind,
        summary=str(raw.get("description", "")),
        keywords=tuple(notes.get("keywords") or ()),
        tactical_uses=tuple(notes.get("tactical_uses") or ()),
        platform_classes=platform_classes,
        ir_v1=tuple(notes.get("ir_v1") or ()),
        limitations=tuple(limitations),
        basis="lab_implemented",
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
        self.tactics: dict[str, dict[str, Any]] = {}  # slice → tactic set (战术集)
        self.source: dict[str, Any] = {}
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
        cls,
        root: Path | str | None = None,
        *,
        allow_private: bool = False,
        alignment: Path | None = None,
    ) -> CapabilityCatalog:
        """``alignment``: the agent-side annotation file (default ``<root>/ir_alignment.toml``)."""
        root = Path(root) if root is not None else default_catalog_root()
        alignment = Path(alignment) if alignment is not None else root / ALIGNMENT_FILE
        index_path = root / "index.yaml"
        if not index_path.exists():
            raise CatalogError(f"no capability set under {root}")
        index = _read_yaml(index_path)
        if index.get("format") != SET_FORMAT:
            raise CatalogError(f"{index_path.name}: not a {SET_FORMAT} index")
        meta = index.get("catalog") or {}
        cls._check_classification(meta, index_path.name, allow_private)
        platform_classes = tuple(meta.get("platform_classes") or ())
        notes = _alignment(alignment)
        files: list[Path] = [index_path]
        caps: dict[str, Capability] = {}
        domains: dict[str, str] = {}
        tactics: dict[str, dict[str, Any]] = {}
        for sl in index.get("slices") or []:
            sid = sl["id"]
            domains[sid] = sl.get("display_name", sid)
            action_path = root / sl["action_file"]
            data = _read_yaml(action_path)
            files.append(action_path)
            info = data.get("slice") or {}
            caps[sid] = _entry_capability(
                {"id": sid, "kind": "slice", "description": info.get("description", "")},
                sid,
                sl["action_file"],
                platform_classes,
                notes.get(sid, {}),
            )
            for key in ("primitives", "actions", "tactic_steps"):
                for raw in data.get(key) or []:
                    if raw["id"] in caps:
                        raise CatalogError(f"duplicate capability id '{raw['id']}'")
                    caps[raw["id"]] = _entry_capability(
                        raw, sid, sl["action_file"], platform_classes, notes.get(raw["id"], {})
                    )
            tactic_path = root / sl["tactic_file"]
            if tactic_path.exists():
                tactics[sid] = _read_yaml(tactic_path)
                files.append(tactic_path)
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
        if alignment.exists():
            digest.update(alignment.read_bytes())
        catalog = cls(
            caps.values(),
            catalog_id=str(meta.get("id", root.name)),
            platform=" ".join(
                str(meta[k]) for k in ("platform_family", "platform_version") if meta.get(k)
            ),
            content_hash=f"sha256:{digest.hexdigest()}",
            domains=domains,
            overrides=overrides,
        )
        catalog.tactics = tactics
        catalog.source = dict(index.get("source") or {})
        return catalog

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
