"""Export the public capability set (动作集 + 战术集) from the vehicle lab project.

The lab project (e.g. ``apc_behavior_lab``) is the master copy. Its platform-independent
layers are normalised into one format and written to the repository:

    <out>/index.yaml                 catalog metadata, slices, counts, source hash
    <out>/action_set/<slice>.yaml    resources, blackboard, primitives, actions, tactic steps
    <out>/tactic_set/<slice>.yaml    tactic subtrees, state trees, missions, conditions

Read from the lab, per slice directory name::

    catalog/<slice>*_capabilities.yaml    platform primitives (read / measure / command)
    actions/<slice>/*.action.yaml         action contracts
    schema/blackboard/<slice>*.yaml       blackboard fields
    arbitration/<slice>.policy.yaml       resources
    tactics/<slice>/*.subtree.yaml        tactic subtrees
    behavior_trees/<slice>/*.tree.yaml    state trees
    missions/*.fsm.yaml                   mission state machines (matched to a slice)
    schema/organization/*.yaml            organization schema

Format 1.1 adds the semantics the lab records (all optional; a 1.0 lab exports as before):

    fields       description, initial, enum_meaning, updated, internal, note; and, derived
                 here, written_by (actions / tactic steps whose ``writes`` list the field)
    actions      reads / writes (with updated, reset_on_start, on_success, on_failure),
                 result (running / success / failure meaning), persistent
    tactic steps contract files with ``kind: tactic_step`` under actions/<slice>/
    subtrees     result, persistent; interface (reads, writes, resources) derived here
    missions     events: <trigger> -> {meaning, description}
    slices       description (from the slice catalog)

Consistency problems are listed as warnings in the report; they never stop the export.

Never read: ``bindings/``, ``model/``, ``scenarios/``, ``fixtures/``, ``tests/``, ``_local``.
Every string that is written is scanned; native names, file paths, handling markings and
calibre-style platform parameters are dropped (fail closed) and listed in the report, which
the caller keeps local.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

FORMAT = "CapabilitySet"
FORMAT_VERSION = "1.1.0"

# how a written field is kept up to date
UPDATED = (
    "constant",
    "state_entry",
    "always",
    "while_running",
    "on_start",
    "on_success",
    "on_failure",
)

SLICES: dict[str, dict[str, Any]] = {
    "movement": {"display_name": "运动", "catalog": "movement", "blackboard": "movement"},
    "perception": {
        "display_name": "感知与跟踪",
        "catalog": "perception",
        "blackboard": "perception",
    },
    "communication_target_sharing": {
        "display_name": "通信与目标共享",
        "catalog": "communication_target_sharing",
        "blackboard": "communication_target_sharing",
        "mission_hint": "target_sharing",
    },
    "threat_allocation": {
        "display_name": "集中式目标分配",
        "catalog": "threat_allocation",
        "blackboard": "threat_allocation",
    },
    "engagement": {
        "display_name": "多武器交战与毁伤",
        "catalog": "engagement",
        "blackboard": "engagement",
        "mission_hint": "engagement",
    },
    "formation_standoff": {
        "display_name": "编队对目标保持距离",
        "catalog": "formation_standoff",
        "blackboard": "formation_standoff",
        "mission_hint": "standoff",
    },
    "hierarchical_formation": {
        "display_name": "连—排分层编队",
        "catalog": "hierarchical_formation",
        "blackboard": "hierarchical_formation",
    },
}

# drop any string that matches (native symbols, paths, markings, platform parameters)
FORBIDDEN = [
    (re.compile(r"\bWsf[A-Z]\w*"), "native_type"),
    (re.compile(r"\bWSF_\w+"), "native_type"),
    (re.compile(r"\bAPC_\w+"), "native_symbol"),
    (re.compile(r"\bapc_\w+"), "native_symbol"),
    (re.compile(r"Aux[A-Z]\w*"), "native_api"),
    (re.compile(r"afsim_2_9\.|\bbindings?/|(^|[/\\])_local\b"), "binding_ref"),
    (re.compile(r"[A-Za-z]:[\\/]|\.txt\b|/afsim/", re.I), "path"),
    (re.compile(r"\bCUI\b"), "marking"),
    (re.compile(r"\d+\s?mm", re.I), "platform_parameter"),
]

FIELD_KEYS = (
    "type",
    "units",
    "unit",
    "enum",
    "values",
    "nullable",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "exclusive_maximum",
    "default",
    "description",
    "minLength",
    "max_items",
    "item_type",
    "const",
    # 1.1 semantics
    "initial",
    "enum_meaning",
    "updated",
    "internal",
    "note",
    # exposed parameters: who supplies the value (design / platform / scenario), its effect
    "category",
    "effect",
)


@dataclass
class ExportReport:
    dropped: list[tuple[str, str, str]] = field(default_factory=list)  # (where, reason, text)
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # (where, ref)
    files_read: int = 0
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (where, problem)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"读取 {self.files_read} 个文件"]
        for sid, c in self.counts.items():
            lines.append(f"  {sid}: " + "，".join(f"{k} {v}" for k, v in c.items()))
        lines.append(f"清除 {len(self.dropped)} 处（仅本地显示）：")
        for where, reason, text in self.dropped:
            lines.append(f"  [{reason}] {where}: {text[:100]}")
        if self.unresolved:
            lines.append(f"无契约的战术步骤 {len(self.unresolved)} 个（登记为 tactic_step）：")
            lines += [f"  {where}: {ref}" for where, ref in self.unresolved]
        if self.warnings:
            lines.append(f"一致性警告 {len(self.warnings)} 条：")
            lines += [f"  {where}: {problem}" for where, problem in self.warnings]
        return "\n".join(lines)


class LabExporter:
    def __init__(self, lab: Path) -> None:
        self.lab = Path(lab)
        if not (self.lab / "actions").is_dir():
            raise FileNotFoundError(f"not a lab project (no actions/): {self.lab}")
        self.report = ExportReport()
        self._pending: dict[str, set[str]] = {}
        self._hash = hashlib.sha256()

    # ------------------------------------------------------------------ io
    def _load(self, path: Path) -> Any:
        if "_local" in path.parts:
            raise PermissionError(path.name)
        data = path.read_bytes()
        self._hash.update(path.relative_to(self.lab).as_posix().encode())
        self._hash.update(data.replace(b"\r\n", b"\n"))
        self.report.files_read += 1
        return yaml.safe_load(data.decode("utf-8")) or {}

    def _files(self, pattern: str) -> list[Path]:
        return sorted(self.lab.glob(pattern), key=lambda p: p.as_posix())

    # ------------------------------------------------------------------ scrubbing
    def clean(self, value: Any, where: str) -> Any:
        """Recursively drop strings (and the keys/items holding them) that are not public."""
        if isinstance(value, str):
            for rx, reason in FORBIDDEN:
                if rx.search(value):
                    self.report.dropped.append((where, reason, value))
                    return None
            return value
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                key = self.clean(str(k), f"{where}.<key>")
                if key is None:
                    continue
                cv = self.clean(v, f"{where}.{k}")
                if cv is None and v is not None:
                    if k == "id":
                        raise ValueError(f"{where}: identifier is not public: {v}")
                    continue
                out[key] = cv
            return out
        if isinstance(value, list):
            return [c for c in (self.clean(v, f"{where}[]") for v in value) if c is not None]
        return value

    # ------------------------------------------------------------------ export
    def export(self) -> dict[str, Any]:
        missions = self._missions()
        result: dict[str, Any] = {"actions": {}, "tactics": {}}
        for sid, spec in SLICES.items():
            actions = self._action_set(sid, spec)
            tactics = self._tactic_set(sid, spec, missions.get(sid, []), actions)
            result["actions"][sid] = actions
            result["tactics"][sid] = tactics
        self._resolve_steps(result)
        self._semantics(result)
        for sid in SLICES:
            a, t = result["actions"][sid], result["tactics"][sid]
            self.report.counts[sid] = {
                "原语": len(a["primitives"]),
                "动作": len(a["actions"]),
                "战术步骤": len(a["tactic_steps"]),
                "子树": len(t["subtrees"]),
                "状态树": len(t["state_trees"]),
                "任务": len(t["missions"]),
            }
        result["index"] = self._index(result)
        return result

    def _resolve_steps(self, result: dict[str, Any]) -> None:
        """Actions referenced by tactics without a contract in any slice become tactic steps
        of the slice that owns their prefix (or the slice that uses them)."""
        known = {
            x["id"] for a in result["actions"].values() for x in a["primitives"] + a["actions"]
        }
        steps = {x["id"]: x for a in result["actions"].values() for x in a["tactic_steps"]}
        for sid, refs in sorted(self._pending.items()):
            for ref in sorted(refs & set(steps)):
                used = steps[ref].setdefault("used_by", [])
                if sid not in used:
                    used.append(sid)
        known |= set(steps)
        for sid, refs in self._pending.items():
            for ref in sorted(refs - known):
                owner = ref.split(".")[0] if ref.split(".")[0] in SLICES else sid
                result["actions"][owner]["tactic_steps"].append(
                    self.clean(
                        {
                            "id": ref,
                            "kind": "tactic_step",
                            "status": "implemented",
                            "description": "战术内部步骤，无独立动作契约",
                            "used_by": [sid],
                        },
                        f"{owner}.tactic_steps",
                    )
                )
                known.add(ref)
                self.report.unresolved.append((sid, ref))

    def _index(self, result: dict[str, Any]) -> dict[str, Any]:
        slices = []
        for sid, spec in SLICES.items():
            a, t = result["actions"][sid], result["tactics"][sid]
            slices.append(
                {
                    "id": sid,
                    "display_name": spec["display_name"],
                    "action_file": f"action_set/{sid}.yaml",
                    "tactic_file": f"tactic_set/{sid}.yaml",
                    **(
                        {"description": a["slice"]["description"]}
                        if a["slice"].get("description")
                        else {}
                    ),
                    **({"semantics": True} if a["slice"].get("semantics") else {}),
                    "counts": {
                        "primitives": len(a["primitives"]),
                        "actions": len(a["actions"]),
                        "tactic_steps": len(a["tactic_steps"]),
                        "subtrees": len(t["subtrees"]),
                        "state_trees": len(t["state_trees"]),
                        "missions": len(t["missions"]),
                    },
                }
            )
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "catalog": {
                "id": "armored_vehicle_afsim_2_9",
                "display_name": "装甲车 AFSIM 2.9 能力集",
                "platform_family": "afsim",
                "platform_version": "2.9",
                "platform_classes": ["ground_vehicle"],
                "classification": "public",
                "status_meaning": (
                    "implemented = 工程中有契约和原生实现，并有对应的运行想定与测试；"
                    "目录外的能力一律视为未实现"
                ),
            },
            "source": {
                "project": "vehicle_lab",
                "exported_on": date.today().isoformat(),
                "content_hash": f"sha256:{self._hash.hexdigest()}",
                "files_read": self.report.files_read,
            },
            "slices": slices,
        }

    # ------------------------------------------------------------------ action set
    def _action_set(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "format": f"{FORMAT}.actions",
            "format_version": FORMAT_VERSION,
            "slice": {"id": sid, "display_name": spec["display_name"]},
            "parameters": {},
            "resources": {},
            "blackboard": {"namespaces": {}, "fields": {}},
            "primitives": [],
            "actions": [],
            "tactic_steps": [],
        }
        for path in self._files(f"catalog/{spec['catalog']}*_capabilities.yaml"):
            data = self._load(path)
            where = f"{sid}.catalog"
            if data.get("description"):
                out["slice"]["description"] = data["description"]
            for key in ("parameters", "limits"):
                if isinstance(data.get(key), dict):
                    out["parameters"].update(data[key])
            if data.get("formations"):
                out["parameters"]["formations"] = list(data["formations"])
            for claim in data.get("resource_claims") or []:
                item = {"mode": claim.get("mode", "exclusive")}
                if claim.get("scope"):
                    item["scope"] = claim["scope"]
                out["resources"].setdefault(claim["resource"], item)
            for raw in data.get("capabilities") or []:
                out["primitives"].append(self._primitive(raw, where))
        pol = self.lab / "arbitration" / f"{sid}.policy.yaml"
        if pol.exists():
            out["resources"].update(self._resources(self._load(pol)))
        for path in self._files(f"schema/blackboard/{spec['blackboard']}*.yaml"):
            self._blackboard(self._load(path), out["blackboard"])
        for path in self._files(f"actions/{sid}/*.action.yaml"):
            item = self._action(self._load(path), f"{sid}.{path.stem}")
            out["tactic_steps" if item["kind"] == "tactic_step" else "actions"].append(item)
        # a primitive that only restates an action with the same id is folded into it
        actions_by_id = {a["id"]: a for a in out["actions"]}
        kept = []
        for p in out["primitives"]:
            a = actions_by_id.get(p["id"])
            if a is None:
                kept.append(p)
                continue
            for key, value in p.items():
                if key not in ("kind", "status") and not a.get(key):
                    a[key] = value
        out["primitives"] = kept
        out = self.clean(out, sid)
        out["primitives"].sort(key=lambda x: x["id"])
        out["actions"].sort(key=lambda x: x["id"])
        out["tactic_steps"].sort(key=lambda x: x["id"])
        return out

    @staticmethod
    def _io(items: Iterable[Any]) -> list[dict[str, Any]]:
        out = []
        for item in items or []:
            if isinstance(item, str):
                out.append({"name": item})
            elif isinstance(item, dict):
                out.append(
                    {
                        k: v
                        for k, v in item.items()
                        if k == "name" or k in FIELD_KEYS or k == "required"
                    }
                )
        return out

    def _primitive(self, raw: dict[str, Any], where: str) -> dict[str, Any]:
        p: dict[str, Any] = {
            "id": raw["id"],
            "kind": "primitive",
            "status": "implemented",
            "description": raw.get("description") or raw.get("name") or "",
        }
        if raw.get("inputs"):
            p["inputs"] = self._io(raw["inputs"])
        if raw.get("outputs"):
            p["outputs"] = self._io(raw["outputs"])
        for key in ("preconditions", "effects", "failure_codes"):
            if raw.get(key):
                p[key] = list(raw[key])
        if raw.get("resource_claims"):
            p["resources"] = [
                {"resource": c["resource"], "mode": c.get("mode", "exclusive")}
                for c in raw["resource_claims"]
            ]
        return p

    @staticmethod
    def _resources(data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        default_lease = data.get("lease_timeout_s")
        for name, r in (data.get("resources") or {}).items():
            item = {"mode": r.get("mode", "exclusive")}
            for k in ("roles", "requires", "conflict_code"):
                if r.get(k):
                    item[k] = r[k]
            lease = r.get("lease_s") or r.get("stale_after_s") or default_lease
            if lease:
                item["lease_s"] = lease
            if r.get("conflict_preserves_current_state"):
                item["conflict_preserves_current_state"] = True
            out[name] = item
        for name in data.get("exclusive_resources") or []:
            out.setdefault(name, {"mode": "exclusive"})
        for role, allowed in (data.get("permissions") or {}).items():
            for name in allowed:
                out.setdefault(name, {"mode": "exclusive"}).setdefault("roles", []).append(role)
        return out

    @staticmethod
    def _blackboard(data: dict[str, Any], into: dict[str, Any]) -> None:
        for ns, spec in (data.get("namespaces") or {}).items():
            if isinstance(spec, dict):
                into["namespaces"][ns] = spec.get("description", "")
            elif isinstance(spec, list):  # engagement: namespace -> field names
                into["namespaces"].setdefault(ns, "")
                for f in spec:
                    into["fields"].setdefault(f"{ns}.{f}", {})
        for name, spec in (data.get("fields") or {}).items():
            spec = spec or {}
            into["fields"][name] = {k: v for k, v in spec.items() if k in FIELD_KEYS}
        for key in ("target_classes",):
            if data.get(key):
                into.setdefault("vocabularies", {})[key] = list(data[key])

    def _action(self, raw: dict[str, Any], where: str) -> dict[str, Any]:
        a: dict[str, Any] = {
            "id": raw["id"],
            "kind": "tactic_step" if raw.get("kind") == "tactic_step" else "action",
            "status": "implemented",
            "description": raw.get("description", ""),
        }
        if raw.get("role"):
            a["roles"] = [raw["role"]]
        statuses = (
            raw.get("statuses")
            or raw.get("status_vocabulary")
            or (raw.get("lifecycle") or {}).get("states")
            or (raw.get("result") if isinstance(raw.get("result"), list) else None)
        )
        if statuses:
            a["statuses"] = list(statuses)
        failures = raw.get("failure_codes") or raw.get("failures")
        if failures:
            a["failure_codes"] = list(failures)
        res: list[dict[str, Any]] = []
        for c in raw.get("resource_claims") or []:
            item = {"resource": c["resource"], "mode": c.get("mode", "exclusive")}
            if c.get("scope"):
                item["scope"] = c["scope"]
            res.append(item)
        sem = raw.get("resource_semantics")
        if isinstance(sem, dict) and sem.get("resource"):
            item = {"resource": sem["resource"], "mode": sem.get("mode", "exclusive")}
            for k in ("acquire", "renew_while_running", "release_on_terminal"):
                if k in sem:
                    item[k] = sem[k]
            res.append(item)
        if isinstance(raw.get("resource"), str):
            res.append({"resource": raw["resource"], "mode": "exclusive"})
        if res:
            a["resources"] = res
        for key in ("preconditions", "effects"):
            if raw.get(key):
                a[key] = list(raw[key])
        params = raw.get("parameters")
        if isinstance(params, dict) and params.get("properties"):
            required = set(params.get("required") or [])
            a["parameters"] = {
                name: {k: v for k, v in (p or {}).items() if k in FIELD_KEYS}
                | ({"required": True} if name in required else {})
                for name, p in params["properties"].items()
            }
        for key in ("success", "cancellation"):
            if isinstance(raw.get(key), dict):
                a[key] = raw[key]
        if isinstance(raw.get("failure"), dict):
            f = raw["failure"]
            a["failure_handling"] = {
                k: f[k]
                for k in (
                    "validate_before_command",
                    "safe_stop_except",
                    "terminal_error_once_per_request",
                )
                if k in f
            }
        bb = raw.get("blackboard")
        if isinstance(bb, dict):
            a["blackboard"] = {k: list(bb.get(k) or []) for k in ("reads", "writes")}
            # 1.1: structured reads / writes
            a["reads"] = list(bb.get("reads") or [])
            sem = bb.get("write_semantics") or {}
            writes = []
            for name in bb.get("writes") or []:
                w: dict[str, Any] = {"field": name}
                updated = (sem.get(name) or {}).get("updated", bb.get("updated"))
                if updated:
                    if updated not in UPDATED:
                        self.report.warnings.append((where, f"未知的更新方式 {updated}（{name}）"))
                    w["updated"] = updated
                for k in ("reset_on_start", "on_success", "on_failure", "only"):
                    if k in (sem.get(name) or {}):
                        w[k] = sem[name][k]
                writes.append(w)
            a["writes"] = writes
        if isinstance(raw.get("result"), dict):
            a["result"] = dict(raw["result"])
        if "persistent" in raw:
            a["persistent"] = bool(raw["persistent"])
        if raw.get("unmapped"):
            a["limitations"] = list(raw["unmapped"])  # scrubbed later; native sentences drop
        return a

    # ------------------------------------------------------------------ semantics (1.1)
    def _semantics(self, result: dict[str, Any]) -> None:
        """Derive written_by / updated per field and the interface of each tree, and list
        consistency problems. Fields of a slice whose contracts are all annotated (every
        action and tactic step states its writes) get ``written_by`` even when empty; a
        field of another slice gets it only when something writes it."""
        contracts = {
            x["id"]: x for a in result["actions"].values() for x in a["actions"] + a["tactic_steps"]
        }
        fields: dict[str, tuple[str, dict[str, Any]]] = {}
        for sid, a in result["actions"].items():
            for name, spec in a["blackboard"]["fields"].items():
                fields.setdefault(name, (sid, spec))
        writers: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for cid, c in contracts.items():
            for w in c.get("writes") or []:
                writers.setdefault(w["field"], []).append((cid, w))
                if w["field"] not in fields:
                    self.report.warnings.append((cid, f"写入的字段不在黑板定义中：{w['field']}"))
            for r in c.get("reads") or []:
                if r not in fields:
                    self.report.warnings.append((cid, f"读取的字段不在黑板定义中：{r}"))
        for sid, a in result["actions"].items():
            items = a["actions"] + a["tactic_steps"]
            annotated = bool(items) and all("writes" in x for x in items)
            if annotated:
                a["slice"]["semantics"] = True
            for name, spec in a["blackboard"]["fields"].items():
                ws = writers.get(name, [])
                if ws or (annotated and spec.get("updated") not in ("constant", "state_entry")):
                    spec["written_by"] = sorted({cid for cid, _ in ws})
                kinds = sorted({w["updated"] for _, w in ws if w.get("updated")})
                if kinds and not spec.get("updated"):
                    spec["updated"] = kinds[0] if len(kinds) == 1 else kinds
                resets = {w["reset_on_start"] for _, w in ws if "reset_on_start" in w}
                if len(resets) == 1:
                    spec["reset_on_start"] = resets.pop()
                if annotated and not spec.get("description"):
                    self.report.warnings.append((sid, f"字段缺少说明：{name}"))
        # tree interfaces
        trees = {
            x["id"]: x for t in result["tactics"].values() for x in t["subtrees"] + t["state_trees"]
        }

        def closure(node: Any, acc: dict[str, set[str]], seen: set[str]) -> None:
            if not isinstance(node, dict):
                return
            if node.get("action") in contracts:
                c = contracts[node["action"]]
                acc["reads"] |= set(c.get("reads") or [])
                acc["writes"] |= {w["field"] for w in c.get("writes") or []}
                acc["resources"] |= {r["resource"] for r in c.get("resources") or []}
            sub = node.get("subtree")
            if sub in trees and sub not in seen:
                seen.add(sub)
                closure(trees[sub].get("root"), acc, seen)
            for child in [node.get("child"), *(node.get("children") or [])]:
                closure(child, acc, seen)

        internal = {n for n, (_, spec) in fields.items() if spec.get("internal")}
        for item in trees.values():
            if "root" not in item:
                continue
            acc: dict[str, set[str]] = {"reads": set(), "writes": set(), "resources": set()}
            closure(item["root"], acc, {item["id"]})
            acc["reads"] -= internal
            acc["writes"] -= internal
            if any(acc.values()):
                item["interface"] = {k: sorted(v) for k, v in acc.items() if v}
        # mission trigger meanings
        for sid, t in result["tactics"].items():
            for m in t["missions"]:
                events = m.get("events")
                if not events:
                    continue
                for tr in m.get("transitions") or []:
                    if tr.get("when") and tr["when"] not in events:
                        self.report.warnings.append((m["id"], f"触发条件缺少含义：{tr['when']}"))
                for name, spec in events.items():
                    for f in sorted(_bb_fields(spec.get("meaning"))):
                        if f not in fields:
                            self.report.warnings.append(
                                (m["id"], f"{name} 读取的字段不在黑板定义中：{f}")
                            )
                        elif fields[f][1].get("written_by") == []:
                            self.report.warnings.append(
                                (m["id"], f"{name} 读取的字段没有写入者：{f}")
                            )

    # ------------------------------------------------------------------ tactic set
    def _missions(self) -> dict[str, list[dict[str, Any]]]:
        """Mission files carry no slice; match them by the state trees they reference or by
        a name hint."""
        found: dict[str, list[dict[str, Any]]] = {}
        for path in self._files("missions/*.fsm.yaml"):
            raw = self._load(path)
            mid = raw.get("id") or path.name.removesuffix(".fsm.yaml")
            sid = self._mission_slice(raw, mid)
            found.setdefault(sid, []).append(self._mission(raw, mid, sid))
        return found

    @staticmethod
    def _mission_slice(raw: dict[str, Any], mid: str) -> str:
        states = raw.get("states")
        if isinstance(states, dict):
            for s in states.values():
                tree = (s or {}).get("tree") if isinstance(s, dict) else None
                if tree and "." in tree:
                    prefix = tree.split(".")[0]
                    if prefix in SLICES:
                        return prefix
        if mid in SLICES:
            return mid
        for sid, spec in SLICES.items():
            if spec.get("mission_hint") and spec["mission_hint"] in mid:
                return sid
            if sid in mid:
                return sid
        return "threat_allocation" if "threat" in mid else "perception"

    @staticmethod
    def _mission(raw: dict[str, Any], mid: str, sid: str) -> dict[str, Any]:
        m: dict[str, Any] = {"id": mid if mid == sid else f"{sid}.{mid}"}
        if raw.get("description"):
            m["description"] = raw["description"]
        m["initial_state"] = raw.get("initial_state")
        if raw.get("parameters"):
            m["parameters"] = raw["parameters"]
        states = raw.get("states") or []
        transitions: list[dict[str, Any]] = []
        trees: dict[str, str] = {}
        terminal: list[str] = []
        if isinstance(states, dict):
            names = list(states)
            for name, s in states.items():
                s = s or {}
                if s.get("tree"):
                    trees[name] = s["tree"]
                if s.get("terminal"):
                    terminal.append(name)
                for t in s.get("transitions") or []:
                    transitions.append({"from": name, "to": t["to"], "when": t.get("on")})
        else:
            names = list(states)
        if isinstance(raw.get("state_trees"), dict):  # flat missions name their state trees
            trees.update({str(k): str(v) for k, v in raw["state_trees"].items()})
        for t in raw.get("transitions") or []:
            transitions.append(
                {"from": t["from"], "to": t["to"], "when": t.get("when") or t.get("event")}
            )
        m["states"] = names
        if terminal or "COMPLETE" in names:
            m["terminal_states"] = terminal or ["COMPLETE"]
        m["transitions"] = transitions
        if raw.get("global_transitions"):
            m["global_transitions"] = [
                {"to": t["to"], "when": t.get("event") or t.get("when")}
                for t in raw["global_transitions"]
            ]
        if trees:
            m["state_trees"] = trees
        if isinstance(raw.get("events"), dict):
            m["events"] = {
                name: {k: v for k, v in (spec or {}).items() if k in ("meaning", "description")}
                for name, spec in raw["events"].items()
            }
        return m

    def _ref(self, sid: str, ref: str | None) -> str | None:
        if not ref:
            return ref
        return ref if "." in ref else f"{sid}.{ref}"

    @staticmethod
    def _parameters(raw: dict[str, Any]) -> dict[str, Any]:
        """Public parameters of a subtree: JSON-Schema style specs (bare values are
        defaults), the same fields as action parameters."""
        out: dict[str, Any] = {}
        for name, p in raw.items():
            if isinstance(p, dict):
                out[name] = {k: v for k, v in p.items() if k in FIELD_KEYS or k == "required"}
            else:
                out[name] = {"default": p}
        return out

    def _node(self, sid: str, n: Any, acc: dict[str, set[str]]) -> Any:
        if not isinstance(n, dict):
            return n
        t = n.get("type")
        out: dict[str, Any] = {"type": t}
        if t == "action":
            ref = self._ref(sid, n.get("ref") or n.get("action"))
            out["action"] = ref
            acc["actions"].add(ref)
            if n.get("operation"):
                out["operation"] = n["operation"]
            if isinstance(n.get("params"), dict) and n["params"]:
                out["params"] = dict(n["params"])
        elif t == "condition":
            ref = n.get("ref") or n.get("condition")
            out["condition"] = ref
            acc["conditions"].add(ref)
        elif t == "subtree":
            ref = self._ref(sid, n.get("ref") or n.get("subtree"))
            out["subtree"] = ref
            acc["subtrees"].add(ref)
            if isinstance(n.get("params"), dict) and n["params"]:
                out["params"] = dict(n["params"])
        if "duration_s" in n:
            out["duration_s"] = n["duration_s"]
        # parallel success policy and repeat settings of the lab's tree vocabulary
        for key in ("success_policy", "threshold", "until_done", "count"):
            if key in n:
                out[key] = n[key]
        if n.get("child"):
            out["child"] = self._node(sid, n["child"], acc)
        if n.get("children"):
            out["children"] = [self._node(sid, c, acc) for c in n["children"]]
        return out

    def _tactic_set(
        self,
        sid: str,
        spec: dict[str, Any],
        missions: list[dict[str, Any]],
        actions: dict[str, Any],
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "format": f"{FORMAT}.tactics",
            "format_version": FORMAT_VERSION,
            "slice": {"id": sid, "display_name": spec["display_name"]},
            "subtrees": [],
            "state_trees": [],
            "missions": missions,
            "conditions": [],
        }
        all_conditions: set[str] = set()
        all_actions: set[str] = set()
        for path in self._files(f"tactics/{sid}/*.subtree.yaml"):
            raw = self._load(path)
            acc: dict[str, set[str]] = {"actions": set(), "conditions": set(), "subtrees": set()}
            item = {"id": self._ref(sid, raw["id"])}
            if raw.get("description"):
                item["description"] = raw["description"]
            for key in ("selection_policy", "no_target_result"):
                if raw.get(key):
                    item[key] = raw[key]
            if isinstance(raw.get("result"), dict):
                item["result"] = dict(raw["result"])
            if "persistent" in raw:
                item["persistent"] = bool(raw["persistent"])
            if isinstance(raw.get("parameters"), dict) and raw["parameters"]:
                item["parameters"] = self._parameters(raw["parameters"])
            item["root"] = self._node(sid, raw.get("root"), acc)
            item["uses"] = {k: sorted(v) for k, v in acc.items() if v}
            all_conditions |= acc["conditions"]
            all_actions |= acc["actions"]
            out["subtrees"].append(item)
        for path in self._files(f"behavior_trees/{sid}/*.tree.yaml"):
            raw = self._load(path)
            acc = {"actions": set(), "conditions": set(), "subtrees": set()}
            item = {"id": self._ref(sid, raw["id"])}
            if raw.get("role"):
                item["role"] = raw["role"]
            if raw.get("description"):
                item["description"] = raw["description"]
            if isinstance(raw.get("parameters"), dict) and raw["parameters"]:
                item["parameters"] = self._parameters(raw["parameters"])
            if isinstance(raw.get("states"), dict):  # one file per role: state -> subtree
                item["states"] = {s: self._ref(sid, t) for s, t in raw["states"].items()}
            else:
                item["root"] = self._node(sid, raw.get("root"), acc)
                item["uses"] = {k: sorted(v) for k, v in acc.items() if v}
            all_conditions |= acc["conditions"]
            all_actions |= acc["actions"]
            out["state_trees"].append(item)
        if sid == "hierarchical_formation":
            for path in self._files("schema/organization/*.yaml"):
                raw = self._load(path)
                out["organization"] = {
                    k: raw[k]
                    for k in (
                        "limits",
                        "configured_roles",
                        "runtime_roles",
                        "required_member_fields",
                        "validation",
                    )
                    if k in raw
                }
        out["conditions"] = sorted(c for c in all_conditions if c)
        self._pending[sid] = {a for a in all_actions if a}
        out = self.clean(out, f"{sid}.tactics")
        out["subtrees"].sort(key=lambda x: x["id"])
        out["state_trees"].sort(key=lambda x: x["id"])
        return out


def _bb_fields(expr: Any) -> set[str]:
    """Blackboard fields read by an IR expression (``{bb: name}`` anywhere inside)."""
    out: set[str] = set()
    if isinstance(expr, dict):
        for k, v in expr.items():
            if k == "bb" and isinstance(v, str):
                out.add(v)
            else:
                out |= _bb_fields(v)
    elif isinstance(expr, list):
        for v in expr:
            out |= _bb_fields(v)
    return out


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def write_export(result: dict[str, Any], out: Path) -> list[Path]:
    """Write the export; refuses to write outside ``out`` and replaces only set files."""
    out = Path(out)
    (out / "action_set").mkdir(parents=True, exist_ok=True)
    (out / "tactic_set").mkdir(parents=True, exist_ok=True)
    written = []
    files = {"index.yaml": result["index"]}
    for sid, data in result["actions"].items():
        files[f"action_set/{sid}.yaml"] = data
    for sid, data in result["tactics"].items():
        files[f"tactic_set/{sid}.yaml"] = data
    header = (
        "# 由装甲车工程导出的正式层（动作集 / 战术集）。不要手工修改：在工程中修改后重新导出。\n"
        "# python -m afsim_research_agent.capability export-lab --lab <工程目录>\n"
    )
    for rel, data in files.items():
        path = out / rel
        path.write_text(header + _dump(data), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def scan_tree(root: Path) -> list[tuple[str, str, str]]:
    """Re-scan written files (used by tests and as a final check)."""
    hits = []
    for path in sorted(Path(root).rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for rx, reason in FORBIDDEN:
                if rx.search(line):
                    hits.append((path.name, reason, line.strip()))
    return hits
