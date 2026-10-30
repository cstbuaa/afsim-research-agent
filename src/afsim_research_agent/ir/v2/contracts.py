"""Lookups over a capability set for IR 2.0: action contracts, tactic-set entries,
slice blackboards and resources."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.capability import CapabilityCatalog
from afsim_research_agent.capability.catalog import ALIGNMENT_FILE

# contract unit → IR dimension and unit (the capability set writes JSON-Schema style units)
CONTRACT_UNITS: dict[str, tuple[str, str]] = {
    "m": ("distance", "m"),
    "s": ("time", "s"),
    "m/s": ("speed", "m/s"),
    "deg": ("angle", "deg"),
    "deg_true": ("angle", "deg"),
}
# contract field type → IR value type
FIELD_TYPES = {
    "number": "double",
    "integer": "int",
    "boolean": "bool",
    "string": "string",
    "enum": "string",
}


# resource scopes that name other vehicles than the one running the action
OTHER_VEHICLES = ("followers", "members", "others")

# fields kept up to date without a writing action
STATIC_UPDATES = ("constant", "state_entry", "always")


@dataclass(frozen=True)
class BBField:
    path: str
    type: str  # bool / int / double / string / any
    dimension: str | None = None  # physical dimension when the field has units
    slice: str | None = None  # None: declared by the model (mission.*)
    enum: tuple[str, ...] = ()
    nullable: bool = False
    default: Any = None
    # capability set 1.1 semantics (None / empty: not stated)
    description: str = ""
    written_by: tuple[str, ...] | None = None  # () : nothing writes it
    updated: str | tuple[str, ...] | None = None
    initial: Any = None
    has_initial: bool = False
    reset_on_start: Any = None
    internal: bool = False
    note: str = ""
    enum_meaning: tuple[tuple[str, str], ...] = ()

    @property
    def unwritten(self) -> bool:
        """The capability set says nothing writes this field (it cannot drive a condition)."""
        return self.written_by == () and self.updated not in STATIC_UPDATES


@dataclass(frozen=True)
class ActionContract:
    id: str
    slice: str
    kind: str  # action / tactic_step
    status: str
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    resources: tuple[dict[str, Any], ...] = ()
    failure_codes: tuple[str, ...] = ()
    platform_classes: tuple[str, ...] = ()
    success: dict[str, Any] = field(default_factory=dict)  # completion criteria of the contract
    # capability set 1.1: reads / writes (field, updated, reset_on_start, ...), result meaning
    description: str = ""
    reads: tuple[str, ...] = ()
    writes: tuple[dict[str, Any], ...] | None = None  # None: not stated
    result: dict[str, Any] = field(default_factory=dict)
    persistent: bool | None = None

    @property
    def written(self) -> set[str]:
        return {str(w.get("field")) for w in self.writes or ()}

    @property
    def exclusive(self) -> set[str]:
        """Exclusive resources of the vehicle running the action. A claim scoped to other
        vehicles (``scope: followers``: a formation step commanding the followers'
        locomotion) does not compete with the vehicle's own actions."""
        return {
            r["resource"]
            for r in self.resources
            if r.get("mode") == "exclusive" and r.get("scope") not in OTHER_VEHICLES
        }


@dataclass(frozen=True)
class TacticEntry:
    id: str
    slice: str
    section: str  # subtrees / state_trees / missions
    raw: dict[str, Any]

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(self.raw.get("parameters") or {})


class Contracts:
    """Everything IR 2.0 normalization needs from one capability set."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        blackboard_aliases: dict[str, str] | None = None,
        semantics: SimSemantics | None = None,
    ):
        self.catalog = catalog
        self.actions: dict[str, ActionContract] = {}
        for cap in catalog.capabilities.values():
            if cap.kind not in ("action", "tactic_step"):
                continue
            raw = cap.raw
            self.actions[cap.id] = ActionContract(
                id=cap.id,
                slice=cap.domain,
                kind=cap.kind,
                status=cap.status,
                parameters=dict(raw.get("parameters") or {}),
                resources=tuple(r for r in raw.get("resources") or [] if isinstance(r, dict)),
                failure_codes=tuple(raw.get("failure_codes") or ()),
                platform_classes=cap.platform_classes,
                success=dict(raw.get("success") or {}),
                description=str(raw.get("description") or ""),
                reads=tuple(str(r) for r in raw.get("reads") or ()),
                writes=(
                    tuple(w for w in raw["writes"] if isinstance(w, dict))
                    if isinstance(raw.get("writes"), list)
                    else None
                ),
                result=dict(raw.get("result") or {}) if isinstance(raw.get("result"), dict) else {},
                persistent=raw.get("persistent")
                if isinstance(raw.get("persistent"), bool)
                else None,
            )
        self.tactics: dict[str, TacticEntry] = {}
        for sid, data in catalog.tactics.items():
            for section in ("subtrees", "state_trees", "missions"):
                for raw in data.get(section) or []:
                    if isinstance(raw, dict) and raw.get("id"):
                        self.tactics[raw["id"]] = TacticEntry(raw["id"], sid, section, raw)
        self.blackboard: dict[str, BBField] = {}
        self.resources: dict[str, dict[str, Any]] = {}
        for sid, data in catalog.slices.items():
            for path, spec in ((data.get("blackboard") or {}).get("fields") or {}).items():
                if path not in self.blackboard:
                    self.blackboard[path] = _bb_field(path, spec, sid)
            for name, spec in (data.get("resources") or {}).items():
                self.resources.setdefault(name, dict(spec or {}))
        self.blackboard_aliases = dict(blackboard_aliases or {})
        self.semantics = semantics or SimSemantics()
        self._merge_catalog_semantics()

    def _merge_catalog_semantics(self) -> None:
        """Trigger meanings and initial values recorded in the capability set (1.1) are the
        master copy; the alignment file only fills what the capability set leaves out."""
        for entry in self.tactics.values():
            events = entry.raw.get("events") if entry.section == "missions" else None
            if not isinstance(events, dict):
                continue
            table = self.semantics.events.setdefault(entry.id, {})
            for name, spec in events.items():
                meaning = (spec or {}).get("meaning")
                if isinstance(meaning, dict) and meaning:
                    table[str(name)] = dict(meaning)
        for path, f in self.blackboard.items():
            if f.has_initial and path not in self.semantics.initial:
                self.semantics.initial[path] = f.initial

    def writers(self, path: str) -> tuple[str, ...] | None:
        f = self.blackboard.get(path)
        return None if f is None else f.written_by

    @classmethod
    def load(cls, root: Path | str | None = None) -> Contracts:
        from afsim_research_agent.capability import default_catalog_root

        base = Path(root) if root is not None else default_catalog_root()
        catalog = CapabilityCatalog.load(base)
        alignment = base / ALIGNMENT_FILE
        return cls(catalog, load_blackboard_aliases(alignment), SimSemantics.load(alignment))

    def slice_fields(self, slices: set[str]) -> dict[str, BBField]:
        return {p: f for p, f in self.blackboard.items() if f.slice in slices}


def _bb_field(path: str, spec: Any, slice_id: str) -> BBField:
    spec = spec if isinstance(spec, dict) else {}
    kind = FIELD_TYPES.get(str(spec.get("type")), "any")
    if "enum" in spec and kind == "any":
        kind = "string"
    unit = spec.get("units")
    dimension = CONTRACT_UNITS.get(str(unit), (None, None))[0] if unit else None
    return BBField(
        path=path,
        type=kind,
        dimension=dimension,
        slice=slice_id,
        enum=tuple(str(v) for v in spec.get("enum") or () if v is not None),
        nullable=bool(spec.get("nullable")),
        default=spec.get("default"),
        description=str(spec.get("description") or ""),
        written_by=(
            tuple(str(w) for w in spec["written_by"])
            if isinstance(spec.get("written_by"), list)
            else None
        ),
        updated=(
            tuple(spec["updated"]) if isinstance(spec.get("updated"), list) else spec.get("updated")
        ),
        initial=spec.get("initial"),
        has_initial="initial" in spec,
        reset_on_start=spec.get("reset_on_start"),
        internal=bool(spec.get("internal")),
        note=str(spec.get("note") or ""),
        enum_meaning=tuple((str(k), str(v)) for k, v in (spec.get("enum_meaning") or {}).items()),
    )


def load_blackboard_aliases(path: Path) -> dict[str, str]:
    """``[blackboard]`` of the alignment file: IR 1.0 operand → blackboard field."""
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (data.get("blackboard") or {}).items()}


@dataclass
class SimSemantics:
    """How the 2D simulation executes a capability set (from the alignment file).

    ``[sim."<action id>"]``: ``model`` names a generic action model of
    :mod:`afsim_research_agent.ir.v2.sim.actions`; other keys are its options.
    ``[tactic_condition."<name>"]``: ``expression`` (IR expression) or ``event`` (an
    external event that is latched) — conditions used inside tactic-set trees.
    ``[sim_fields]``: generic key of the action models → blackboard field.
    ``[event."<tactic-set mission>"]``: trigger text → ``{external = true}``,
    ``{expression = …}``, ``{tree = "success" | "failure"}`` or ``{after_s = …}``.
    """

    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    tactic_conditions: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)  # generic model key → blackboard field
    # ``[sim_initial]``: field → value at the start of L2 tests and L3 runs (e.g. formation
    # slot errors start large: the followers are not in their slots yet)
    initial: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SimSemantics:
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            actions={str(k): dict(v) for k, v in (data.get("sim") or {}).items()},
            tactic_conditions={
                str(k): dict(v) for k, v in (data.get("tactic_condition") or {}).items()
            },
            events={
                str(m): {str(t): dict(v) for t, v in table.items()}
                for m, table in (data.get("event") or {}).items()
            },
            fields={str(k): str(v) for k, v in (data.get("sim_fields") or {}).items()},
            initial=dict(data.get("sim_initial") or {}),
        )

    def event(self, mission: str | None, text: str | None) -> dict[str, Any] | None:
        if not mission or text is None:
            return None
        return self.events.get(mission, {}).get(text)
