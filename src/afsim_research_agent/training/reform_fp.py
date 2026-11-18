"""Formation-change skeleton (family ``reform``): a platoon leader orders a sequence of
formations, each held for a while; the members follow their slots.

The layout is the one the 11-18 platform check ran in AFSIM (all six formations formed):

- the leader: INIT (load the organization) → one state per formation (``platoon_plan_formation``
  with the formation, spacing and lateral distance; the next formation after ``hold_s`` — a
  timer, never ``formation.quality.in_position``, which only the members write) → after the
  last formation it holds until the task ends → SHUTDOWN → COMPLETE;
- the members: INIT → FOLLOW (``member_follow_slot`` on every update, nothing in sequence
  after it, so a member keeps following when the slot moves) → SHUTDOWN → COMPLETE;
- any state: ``cancelled`` → SHUTDOWN.

The sheet carries L2 tests and one obligation per formation (``formation_shape``, judged on
the AFSIM positions), so a run whose members never reach their slots fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

FORMATIONS = {
    "column": "COLUMN",
    "line_abreast": "LINE_ABREAST",
    "wedge_forward": "WEDGE_FORWARD",
    "wedge_rear": "WEDGE_REAR",
    "echelon_left": "ECHELON_LEFT",
    "echelon_right": "ECHELON_RIGHT",
}
STATE = {
    "column": "F_COLUMN",
    "line_abreast": "F_LINE",
    "wedge_forward": "F_WEDGE_FWD",
    "wedge_rear": "F_WEDGE_REAR",
    "echelon_left": "F_ECH_LEFT",
    "echelon_right": "F_ECH_RIGHT",
}
NAMES = {
    "column": "纵队",
    "line_abreast": "横队",
    "wedge_forward": "楔形（僚车在后两侧）",
    "wedge_rear": "倒楔形（僚车在前两侧）",
    "echelon_left": "左梯队",
    "echelon_right": "右梯队",
}


@dataclass
class ReformSpec:
    formations: list[str] = field(default_factory=lambda: ["column", "line_abreast"])
    hold_s: float = 60.0
    spacing_m: float = 50.0
    lateral_m: float | None = None
    names: dict[str, str] = field(default_factory=dict)  # leader / member → role
    event_text: dict[str, str] = field(default_factory=dict)
    members: int = 2

    def name(self, role: str) -> str:
        return self.names.get(role, role)

    @property
    def lateral(self) -> float:
        return float(self.lateral_m or self.spacing_m)

    def states(self) -> list[str]:
        """Formation states in order; a formation repeated later gets its own state."""
        out: list[str] = []
        for f in self.formations:
            base = STATE[f]
            name, k = base, 2
            while name in out:
                name, k = f"{base}_{k}", k + 1
            out.append(name)
        return out


MEMBER_TREES = """def INIT():
    tactic("hierarchical_formation.initialize_hierarchical_organization")

def FOLLOW():
    tactic("hierarchical_formation.member_follow_slot", speed_mps=8, tolerance_m=10)

def SHUTDOWN():
    tactic("hierarchical_formation.safe_hierarchical_shutdown")"""


def leader_states(s: ReformSpec) -> list[str]:
    return ["INIT", *s.states(), "SHUTDOWN", "COMPLETE"]


def member_states(s: ReformSpec) -> list[str]:
    return ["INIT", "FOLLOW", "SHUTDOWN", "COMPLETE"]


def sheet(s: ReformSpec, extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The tactic sheet and its request (``extra``: model-block fields to add/replace)."""
    lead, mem = s.name("leader"), s.name("member")
    fs = s.states()
    model: dict[str, Any] = {
        "model": {
            "display_name": "队形变换骨架",
            "intent": "排长车依次下达队形命令，僚车按槽位跟随",
        },
        "slices": ["hierarchical_formation"],
        "roles": {
            lead: {"capability_class": "ground_vehicle", "count": 1,
                   "description": "排长车；依次下达队形命令，自己不入槽"},
            mem: {"capability_class": "ground_vehicle", "count": s.members,
                  "description": "僚车；持续驶向并保持排长车分配的槽位"},
        },
        "events": {
            "cancelled": {"source": "external",
                          "description": s.event_text.get("cancelled", "上级结束任务")},
            "org_ready": {"source": "tree", "result": "success", "description": "编制初始化完成"},
            "hold_up": {"after": f"{s.hold_s:g} s",
                        "description": f"每种队形保持 {s.hold_s:g} s 后变换下一个"},
            "shutdown_done": {"source": "tree", "result": "success", "description": "收尾完成"},
        },
    }  # fmt: skip
    for k, v in (extra or {}).items():
        if v is not None:
            model[k] = v
    chain = ["  [*] --> INIT", f"  INIT --> {fs[0]} : org_ready"]
    chain += [f"  {a} --> {b} : hold_up" for a, b in zip(fs, fs[1:], strict=False)]
    chain += ["  SHUTDOWN --> COMPLETE : shutdown_done", "  * --> SHUTDOWN : cancelled",
              "  COMPLETE --> [*]"]  # fmt: skip
    trees = [
        'def INIT():\n    tactic("hierarchical_formation.initialize_hierarchical_organization")'
    ]
    for st, f in zip(fs, s.formations, strict=True):
        trees.append(
            f'def {st}():\n    tactic("hierarchical_formation.platoon_plan_formation", '
            f'formation="{FORMATIONS[f]}", spacing_m={s.spacing_m:g}, lateral_m={s.lateral:g}, '
            "command_valid_s=600)"
        )
    trees.append('def SHUTDOWN():\n    tactic("hierarchical_formation.safe_hierarchical_shutdown")')
    tests: list[dict[str, Any]] = [
        {"id": f"t_{lead}_init", "given": {"role": lead, "state": "INIT", "events": ["org_ready"]},
         "expect": {"next_state": fs[0]}},
        {"id": f"t_{mem}_init", "given": {"role": mem, "state": "INIT", "events": ["org_ready"]},
         "expect": {"next_state": "FOLLOW"}},
    ]  # fmt: skip
    for a, b in zip(fs, fs[1:], strict=False):
        tests.append(
            {"id": f"t_{lead}_{a.lower()}_{b.lower()}",
             "given": {"role": lead, "state": a, "time_in_state_s": s.hold_s + 1,
                       "events": ["hold_up"]},
             "expect": {"next_state": b}}
        )  # fmt: skip
    for role, first in ((lead, fs[0]), (mem, "FOLLOW")):
        tests.append(
            {"id": f"t_{role}_cancelled",
             "given": {"role": role, "state": first, "events": ["cancelled"]},
             "expect": {"next_state": "SHUTDOWN"}}
        )  # fmt: skip
    parts = [
        f"# 战术稿：队形变换（{'→'.join(NAMES[f] for f in s.formations)}）",
        "```yaml model\n" + yaml.safe_dump(model, allow_unicode=True, sort_keys=False) + "```",
        "```mermaid " + lead + "\nstateDiagram-v2\n" + "\n".join(chain) + "\n```",
        "```mermaid " + mem + "\nstateDiagram-v2\n  [*] --> INIT\n  INIT --> FOLLOW : org_ready\n"
        "  SHUTDOWN --> COMPLETE : shutdown_done\n  * --> SHUTDOWN : cancelled\n"
        "  COMPLETE --> [*]\n```",
        "```python " + lead + "\n" + "\n\n".join(trees) + "\n```",
        "```python " + mem + "\n" + MEMBER_TREES + "\n```",
        "```yaml verification\n"
        + yaml.safe_dump({"tests": tests}, allow_unicode=True, sort_keys=False)
        + "```",
    ]
    request = {
        "requirement": "（逆向配文前的占位）",
        "model_id": "reform_skeleton",
        "roles": {lead: {"capability_class": "ground_vehicle", "count": 1},
                  mem: {"capability_class": "ground_vehicle", "count": s.members}},
        "scenario_events": {"cancelled": s.event_text.get("cancelled", "上级结束任务")},
        "extract_obligations": False,
        "obligations": obligations(s),
    }  # fmt: skip
    return "\n\n".join(parts) + "\n", request


def obligations(s: ReformSpec) -> list[dict[str, Any]]:
    """One formation_shape per formation (each must form at some time of the run; the
    formation states follow each other on a timer, so the order is the sheet's)."""
    out = []
    for f in dict.fromkeys(s.formations):
        out.append(
            {
                "id": f"shape_{f}",
                "kind": "formation_shape",
                "description": f"排成{NAMES[f]}（间距 {s.spacing_m:g} m）",
                "shape": f,
                "leader_role": s.name("leader"),
                "spacing_m": s.spacing_m,
                "lateral_m": s.lateral,
                "hold_s": 5.0,
            }
        )
    return out
