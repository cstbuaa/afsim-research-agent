"""K5: capability gap registry, development queue and catalog write-back.

Integration point ⑧ (design/capability-catalog-integration-v1.md §3.6). One registry file
collects the gaps of every run — Crow estimates (``tactics/capability_gaps.yaml``) and IR
checks (``<model package>/capability_gaps.yaml``) — under their stable IDs.

- **Selection.** Crow gaps enter the development queue only when their tactic is selected
  (``select``). Gaps found by an IR check belong to a model that was built, so they are
  selected already.
- **Status.** open → confirmed (the user attests the platform supports it) /
  in_development → implemented (a plugin provides it) / rejected; ``reopen`` goes back.
- **Merging.** Gaps that are the same capability under different wording (typical for
  needs nothing in the catalog matches) are merged into one; the merged IDs stay as
  aliases so later runs land on the surviving record.
- **Write-back.** Confirming or implementing a gap whose contract is a catalog reference
  writes ``capability_overrides.yaml`` next to the catalog; the catalog applies it when
  loaded, so re-annotation and re-checking see the change. Needs with no catalog entry
  are implemented ``--as`` an existing reference (or after adding the capability to the
  catalog).
- **Queue.** Selected, unresolved gaps ordered by blocking type, then how many tactics
  and runs need them, then whether an alternative exists.

The registry stores only formal-layer content (contracts, element IDs, test IDs); native
names and implementation hints stay in the local layer.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from afsim_research_agent.capability.catalog import (
    OVERRIDES_FILE,
    CapabilityCatalog,
    CatalogError,
    default_catalog_root,
)

REGISTRY_ENV = "CROW_GAP_REGISTRY"
DEFAULT_REGISTRY = Path("data") / "capability" / "gap_registry.yaml"
Status = Literal["open", "confirmed", "in_development", "implemented", "rejected"]
STATUS_LABELS = {
    "open": "待处理",
    "confirmed": "已确认平台支持",
    "in_development": "开发中",
    "implemented": "已实现",
    "rejected": "不实现",
}
GAP_LABELS = {"confirm": "需确认", "ir_extension": "需扩展 IR", "develop": "需开发"}
_GAP_ORDER = {"develop": 2, "ir_extension": 1, "confirm": 0}
_TRANSITIONS: dict[str, set[str]] = {
    "confirm": {"open"},
    "start": {"open"},
    "implement": {"open", "in_development"},
    "reject": {"open", "in_development"},
    "reopen": {"confirmed", "in_development", "implemented", "rejected"},
}
_TARGET = {
    "confirm": "confirmed",
    "start": "in_development",
    "implement": "implemented",
    "reject": "rejected",
    "reopen": "open",
}


class RegistryError(ValueError):
    pass


class Occurrence(BaseModel):
    """Where a gap was found: one Crow candidate or one IR model."""

    phase: Literal["crow_estimate", "ir_check"]
    run: str = Field(description="Directory of the gaps file (a tactics dir or model package).")
    subject: str = Field(description="Crow candidate ID, or the model package name.")
    used_at: list[str] = Field(default_factory=list)
    selected: bool = False


class Event(BaseModel):
    on: str
    action: str
    by: str = ""
    note: str = ""


class GapRecord(BaseModel):
    id: str
    contract: str
    display_name: str
    gap_type: Literal["confirm", "ir_extension", "develop"]
    status: Status = "open"
    occurrences: list[Occurrence] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list, description="IDs merged into this one.")
    merged_into: str | None = None
    resolved_as: str | None = Field(
        default=None, description="Catalog reference that now provides the capability."
    )
    history: list[Event] = Field(default_factory=list)

    @property
    def selected(self) -> bool:
        return any(o.selected for o in self.occurrences)

    @property
    def tactics(self) -> list[str]:
        return sorted({f"{o.run}#{o.subject}" for o in self.occurrences if o.selected})

    @property
    def runs(self) -> list[str]:
        return sorted({o.run for o in self.occurrences})


class GapRegistry(BaseModel):
    format: str = "CapabilityGapRegistry"
    format_version: str = "0.1.0"
    gaps: list[GapRecord] = Field(default_factory=list)

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> GapRegistry:
        if not path.is_file():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude_defaults=False)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
        )

    # ------------------------------------------------------------------ lookup
    def get(self, gap_id: str) -> GapRecord | None:
        """The record for an ID, following merges and aliases."""
        record = next((g for g in self.gaps if g.id == gap_id), None)
        if record is None:
            record = next((g for g in self.gaps if gap_id in g.aliases), None)
        seen: set[str] = set()
        while record is not None and record.merged_into and record.id not in seen:
            seen.add(record.id)
            record = next((g for g in self.gaps if g.id == record.merged_into), None)
        return record

    def require(self, gap_id: str) -> GapRecord:
        record = self.get(gap_id)
        if record is None:
            raise RegistryError(f"no gap '{gap_id}' in the registry")
        return record

    def active(self) -> list[GapRecord]:
        return [g for g in self.gaps if g.merged_into is None]

    # ------------------------------------------------------------------ ingest
    def ingest(self, gaps_file: Path) -> list[str]:
        """Add the gaps of a Crow or IR ``capability_gaps.yaml``; idempotent per run."""
        data = yaml.safe_load(gaps_file.read_text(encoding="utf-8")) or {}
        run = str(gaps_file.parent)
        touched: list[str] = []
        ir_phase = data.get("phase") == "ir_check"
        for item in data.get("gaps") or []:
            record = self._record(item)
            if ir_phase:
                occurrences = [
                    Occurrence(
                        phase="ir_check",
                        run=run,
                        subject=gaps_file.parent.name,
                        used_at=list(item.get("used_at") or []),
                        selected=True,  # a model was built from a chosen tactic
                    )
                ]
                _extend(record.acceptance, item.get("acceptance") or [])
                _extend(record.alternatives, item.get("alternatives") or [])
            else:
                occurrences = [
                    Occurrence(
                        phase="crow_estimate",
                        run=run,
                        subject=str(use.get("candidate")),
                        used_at=list(use.get("sources") or []),
                        selected=bool(item.get("selected")),
                    )
                    for use in item.get("used_by") or []
                ]
            for occ in occurrences:
                old = next(
                    (
                        o
                        for o in record.occurrences
                        if (o.phase, o.run, o.subject) == (occ.phase, occ.run, occ.subject)
                    ),
                    None,
                )
                if old is None:
                    record.occurrences.append(occ)
                else:
                    old.used_at = occ.used_at
                    old.selected = old.selected or occ.selected
            touched.append(record.id)
        return touched

    def _record(self, item: dict[str, Any]) -> GapRecord:
        gap_id = str(item["id"])
        record = self.get(gap_id)
        gap_type = str(item.get("gap_type", "develop"))
        if record is None:
            record = GapRecord(
                id=gap_id,
                contract=str(item.get("contract", gap_id)),
                display_name=str(item.get("display_name", item.get("contract", gap_id))),
                gap_type=gap_type,  # type: ignore[arg-type]
            )
            self.gaps.append(record)
        elif _GAP_ORDER.get(gap_type, 0) > _GAP_ORDER[record.gap_type]:
            record.gap_type = gap_type  # type: ignore[assignment]
        return record

    def select(self, gaps_file: Path, candidate: str, by: str = "") -> list[str]:
        """Mark one Crow candidate as the chosen tactic: its gaps enter the queue."""
        run = str(gaps_file.parent)
        self.ingest(gaps_file)
        chosen = []
        for record in self.active():
            for occ in record.occurrences:
                if occ.phase == "crow_estimate" and occ.run == run and occ.subject == candidate:
                    if not occ.selected:
                        occ.selected = True
                        record.history.append(_event("select", by, f"{candidate}（{run}）"))
                    chosen.append(record.id)
        if not chosen:
            raise RegistryError(f"candidate '{candidate}' has no gaps in {gaps_file}")
        _mark_selected(gaps_file, candidate)
        return chosen

    # ------------------------------------------------------------------ status
    def transition(
        self,
        gap_id: str,
        action: str,
        *,
        by: str,
        note: str = "",
        plugin: str | None = None,
        as_ref: str | None = None,
        catalog_root: Path | None = None,
    ) -> GapRecord:
        if action not in _TRANSITIONS:
            raise RegistryError(f"unknown action '{action}'")
        if not by:
            raise RegistryError("who made the decision is required (--by)")
        record = self.require(gap_id)
        if record.status not in _TRANSITIONS[action]:
            raise RegistryError(
                f"{record.id} is {record.status}; '{action}' needs one of "
                f"{sorted(_TRANSITIONS[action])}"
            )
        root = catalog_root or default_catalog_root()
        if action in ("confirm", "implement"):
            ref = as_ref or (record.contract if _is_reference(root, record.contract) else None)
            if ref is None:
                raise RegistryError(
                    f"{record.id} ({record.contract}) has no catalog entry; implement it --as an "
                    "existing reference, or add the capability to the catalog first"
                )
            if action == "confirm" and record.gap_type == "develop" and as_ref is None:
                raise RegistryError(
                    f"{record.id} is missing on the platform; use 'implement' once a plugin "
                    "provides it"
                )
            basis = "user_attested" if action == "confirm" else "plugin_implemented"
            write_override(
                root,
                ref,
                basis=basis,
                gap=record.id,
                by=by,
                note=note or (f"plugin {plugin}" if plugin else ""),
            )
            record.resolved_as = ref
        elif action == "reopen" and record.resolved_as:
            remove_override(root, record.id)
            record.resolved_as = None
        record.status = _TARGET[action]  # type: ignore[assignment]
        detail = "；".join(filter(None, [note, f"插件 {plugin}" if plugin else "", as_ref or ""]))
        record.history.append(_event(action, by, detail))
        return record

    def merge(self, ids: Iterable[str], into: str, *, by: str, note: str = "") -> GapRecord:
        """Treat several gaps as one capability (e.g. differently worded sector-geometry needs)."""
        target = self.require(into)
        for gap_id in ids:
            record = self.require(gap_id)
            if record.id == target.id:
                continue
            for occ in record.occurrences:
                same = next(
                    (
                        o
                        for o in target.occurrences
                        if (o.phase, o.run, o.subject) == (occ.phase, occ.run, occ.subject)
                    ),
                    None,
                )
                if same is None:
                    target.occurrences.append(occ.model_copy())
                else:
                    _extend(same.used_at, occ.used_at)
                    same.selected = same.selected or occ.selected
            _extend(target.acceptance, record.acceptance)
            _extend(target.alternatives, record.alternatives)
            _extend(target.aliases, [record.id, *record.aliases])
            if _GAP_ORDER[record.gap_type] > _GAP_ORDER[target.gap_type]:
                target.gap_type = record.gap_type
            record.merged_into = target.id
            record.history.append(_event("merge", by, f"并入 {target.id}"))
        target.history.append(_event("merge", by, note or f"并入 {', '.join(ids)}"))
        return target

    # ------------------------------------------------------------------ queue
    def queue(self) -> list[GapRecord]:
        """Selected, unresolved gaps in development order."""
        items = [g for g in self.active() if g.selected and g.status in ("open", "in_development")]
        return sorted(
            items,
            key=lambda g: (
                -_GAP_ORDER[g.gap_type],
                -len(g.tactics),
                -len(g.runs),
                bool(g.alternatives),
                g.id,
            ),
        )

    def resolutions(self) -> dict[str, str]:
        """Gap ID (and merged aliases) → the catalog reference now providing it."""
        out: dict[str, str] = {}
        for g in self.active():
            if g.resolved_as and g.status in ("confirmed", "implemented"):
                for gid in (g.id, *g.aliases):
                    out[gid] = g.resolved_as
        return out


# ---------------------------------------------------------------------- helpers
def registry_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv(REGISTRY_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_REGISTRY


def _event(action: str, by: str, note: str = "") -> Event:
    return Event(on=date.today().isoformat(), action=action, by=by, note=note)


def _extend(target: list[str], values: Iterable[str]) -> None:
    for v in values:
        if v not in target:
            target.append(v)


def _is_reference(root: Path, contract: str) -> bool:
    try:
        CapabilityCatalog.load(root).resolve(contract)
    except CatalogError:
        return False
    return True


def _mark_selected(gaps_file: Path, candidate: str) -> None:
    """Record the selection in the Crow gaps file as well (``selected: true``)."""
    data = yaml.safe_load(gaps_file.read_text(encoding="utf-8")) or {}
    changed = False
    for item in data.get("gaps") or []:
        if any(u.get("candidate") == candidate for u in item.get("used_by") or []):
            if not item.get("selected"):
                item["selected"] = True
                changed = True
    if changed:
        gaps_file.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
        )


def write_override(root: Path, ref: str, *, basis: str, gap: str, by: str, note: str = "") -> Path:
    """Add or replace the write-back entry of one gap in ``capability_overrides.yaml``."""
    CapabilityCatalog.load(root).resolve(ref)  # raises CatalogError for unknown references
    path = root / OVERRIDES_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    data = data or {
        "format": "CapabilityOverrides",
        "format_version": "0.1.0",
        "note": "缺口回写：用户确认（user_attested）或插件实现（plugin_implemented）。"
        "由 python -m afsim_research_agent.capability 维护，加载目录时最后应用。",
        "catalog": {"classification": "public"},
        "overrides": [],
    }
    entries = [o for o in data.get("overrides") or [] if o.get("gap") != gap]
    entries.append(
        {
            "ref": ref,
            "basis": basis,
            "gap": gap,
            "by": by,
            "on": date.today().isoformat(),
            **({"note": note} if note else {}),
        }
    )
    data["overrides"] = entries
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )
    return path


def remove_override(root: Path, gap: str) -> None:
    path = root / OVERRIDES_FILE
    if not path.is_file():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["overrides"] = [o for o in data.get("overrides") or [] if o.get("gap") != gap]
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )


def render_queue(registry: GapRegistry) -> str:
    """``dev_queue.md``: what to confirm or develop next, with the delivery contents."""
    queue = registry.queue()
    lines = ["# 平台能力开发队列", ""]
    lines.append(
        "排序：阻塞程度（需开发 > 需扩展 IR > 需确认）> 选定战术数 > 出现的运行数"
        " > 无替代写法优先。只含选定战术或已建模模型的未处理缺口。"
    )
    lines.append("")
    if not queue:
        lines += ["队列为空。", ""]
    else:
        lines += [
            "| # | 缺口 | 能力 | 类型 | 状态 | 选定战术 | 运行数 | 有替代写法 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for i, g in enumerate(queue, 1):
            lines.append(
                f"| {i} | `{g.id}` | {g.display_name} | {GAP_LABELS[g.gap_type]} | "
                f"{STATUS_LABELS[g.status]} | {len(g.tactics)} | {len(g.runs)} | "
                f"{'是' if g.alternatives else '否'} |"
            )
        lines.append("")
        for g in queue:
            lines += [f"## `{g.id}` {g.display_name}", ""]
            lines.append(f"- 契约：`{g.contract}`")
            lines.append(f"- 类型：{GAP_LABELS[g.gap_type]}；状态：{STATUS_LABELS[g.status]}")
            if g.aliases:
                lines.append(f"- 合并的编号：{', '.join(g.aliases)}")
            lines.append(f"- 选定战术：{', '.join(g.tactics) or '—'}")
            used = sorted({u for o in g.occurrences for u in o.used_at})
            lines.append(
                f"- 使用位置：{', '.join(used[:12]) or '—'}" + (" …" if len(used) > 12 else "")
            )
            lines.append(f"- 验收用例：{', '.join(g.acceptance) or '（建模后由 IR 核对补充）'}")
            lines.append(f"- 替代写法：{'；'.join(g.alternatives) or '无'}")
            lines.append("- 实现线索：见本地层 `_local/`（仅授权环境）")
            lines.append("")
    done = [g for g in registry.active() if g.status in ("confirmed", "implemented", "rejected")]
    if done:
        lines += ["## 已处理", "", "| 缺口 | 能力 | 结果 | 回写为 |", "| --- | --- | --- | --- |"]
        for g in done:
            lines.append(
                f"| `{g.id}` | {g.display_name} | {STATUS_LABELS[g.status]} | "
                f"{g.resolved_as or '—'} |"
            )
        lines.append("")
    return "\n".join(lines)
