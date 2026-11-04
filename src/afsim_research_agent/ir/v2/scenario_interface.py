"""Scenario interface and scenario binding: the behaviour model and the scenario are two
layers (design ``scenario-interface-binding-v1``).

A behaviour model names what only a scenario can know — where the vehicle's post is, the
route it takes forward, the orders it may receive — and never writes coordinates or times.

* :class:`ScenarioInterface` lists those names: **points** (with an anchor such as the
  vehicle's start or a point ahead towards the enemy), **routes** (from one point to
  another), **events** (requests and orders; ``required`` ones the model must answer,
  optional ones it may ignore, which means never acting on them) and read-only **facts**.
  :func:`derive` writes it from the requirement text with fixed rules (no model call).
* The model refers to the interface through **scenario parameters** — model parameters
  tagged ``scenario`` (``point:<id>:lat:<anchor>``, ``point:<id>:lon:<anchor>``,
  ``route:<id>:<start>:<end>``) — so the IR needs no new node or parameter type and a model
  carries its own interface. :func:`interface_parameters` declares them.
* :class:`Binding` gives every name a concrete value for one scenario: geodetic and plane
  positions, route geometry, event times, the enemy. :func:`bind` initialises every value
  the scenario does not state with a reasonable default (the vehicles' start, a point a
  few hundred metres towards the enemy, loose arrival tolerances), so a model always runs.
  The 2D simulation and the AFSIM run are both laid out from the same binding.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ANCHORS = ("own_start", "ahead", "toward_enemy")
POINT_PREFIX, ROUTE_PREFIX = "pt_", "rt_"
DEFAULT_ORIGIN = (45.0, -121.0)
DEFAULT_HEADING_DEG = 90.0
DEFAULT_ENEMY_RANGE_M = 650.0
DEFAULT_ROUTE_SPEED_MPS = 8.0
DEFAULT_ARRIVAL_TOLERANCE_M = 20.0  # loose first; tighten later
DEFAULT_END_S = 80.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterfacePoint(_Strict):
    anchor: Literal["own_start", "ahead", "toward_enemy"] = "own_start"
    description: str = ""


class InterfaceRoute(_Strict):
    start: str  # point id
    end: str  # point id
    description: str = ""


class InterfaceEvent(_Strict):
    kind: Literal["request", "command"] = "request"
    required: bool = False
    description: str = ""


class ScenarioInterface(_Strict):
    points: dict[str, InterfacePoint] = Field(default_factory=dict)
    routes: dict[str, InterfaceRoute] = Field(default_factory=dict)
    events: dict[str, InterfaceEvent] = Field(default_factory=dict)
    facts: dict[str, str] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)  # which rule gave what (for review)

    def event_texts(self) -> dict[str, str]:
        return {k: v.description or k for k, v in self.events.items()}

    def optional_events(self) -> set[str]:
        return {k for k, v in self.events.items() if not v.required}

    def check(self) -> list[str]:
        out = []
        for rid, r in self.routes.items():
            for end in (r.start, r.end):
                if end not in self.points:
                    out.append(f"路线 {rid} 的端点 {end} 不是接口中的点")
        return out

    def prompt_text(self) -> str:
        """The interface as the drafter sees it."""
        lines = []
        if self.points:
            lines.append("位置（写 `point=<名字>`，如 `movement.move_to_point(point=post)`）：")
            lines += [
                f"- {k}：{v.description or ANCHOR_TEXT[v.anchor]}" for k, v in self.points.items()
            ]
        if self.routes:
            lines.append("路线（写 `route=<名字>`，如 `movement.follow_route(route=approach)`）：")
            lines += [
                f"- {k}：从 {v.start} 到 {v.end}" + (f"，{v.description}" if v.description else "")
                for k, v in self.routes.items()
            ]
        if self.events:
            lines.append(
                "外部事件（在 model 的 events 中声明为 external；"
                "可选的可以不用，不用即永远不执行）："
            )
            lines += [
                f"- {k}：{v.description}（{'必须响应' if v.required else '可选'}）"
                for k, v in self.events.items()
            ]
        if self.facts:
            lines.append("想定事实（只供理解，不能当作参数数值）：")
            lines += [f"- {k}：{v}" for k, v in self.facts.items()]
        return "\n".join(lines)


ANCHOR_TEXT = {
    "own_start": "车辆出发位置（就是所在阵位）",
    "ahead": "向敌方方向前方的位置",
    "toward_enemy": "向敌方接近到一定距离的位置",
}

# requirement text → interface (fixed rules, checked in order)
_ADVANCE = re.compile(r"前出|推进|抵近|进至|开进|搜索前进|向[^。；，,]{0,8}(机动|前进|开进|推进)")
_AT_POST = re.compile(
    r"担任观察哨|在[^。；，,]{0,10}(警戒|担任|驻守|固守|守卫|设伏)|不要?[^。；，,]{0,4}离开|原地"
)
_ENEMY = re.compile(
    r"[东西南北][方侧面]?[^。；，,]{0,6}?约?\s*[\d.]+\s*(公里|千米|km|米|m)|[东西南北][侧方面]?数百米"
)
COMMANDS = (
    (
        r"(开火|射击|攻击)(命令|指令)|未经(命令|批准|许可)[^。；，,]{0,6}(开火|射击)",
        "fire_order",
        "上级下达开火命令",
    ),
    (r"展开(命令|指令)|(命令|指令)[^。；，,]{0,6}展开", "deploy_order", "上级下达展开命令"),
    (
        r"撤(离|退)(命令|指令)|(命令|指令)[^。；，,]{0,6}撤(离|退)",
        "withdraw_order",
        "上级下达撤离命令",
    ),
)


def derive(requirement: str) -> ScenarioInterface:
    """The interface a requirement implies (fixed rules)."""
    text = requirement or ""
    out = ScenarioInterface()
    out.points["post"] = InterfacePoint(anchor="own_start", description=ANCHOR_TEXT["own_start"])
    if _ADVANCE.search(text):
        out.points["ahead"] = InterfacePoint(
            anchor="ahead", description="前出到达的位置（向敌方方向）"
        )
        out.routes["approach"] = InterfaceRoute(
            start="post", end="ahead", description="从出发位置前出到 ahead 的路线"
        )
        out.sources.append("需求含前出 / 推进：提供 ahead 与前出路线 approach")
    elif _AT_POST.search(text):
        out.sources.append("需求是在阵位警戒 / 观察：车辆已在阵位 post，不提供前出路线")
    else:
        out.sources.append("需求未说明前出：只提供出发位置 post")
    out.events["cancelled"] = InterfaceEvent(kind="request", description="上级结束任务")
    for pattern, eid, desc in COMMANDS:
        if re.search(pattern, text):
            out.events[eid] = InterfaceEvent(kind="command", description=desc)
            out.sources.append(f"需求提到命令：{eid}（可选；不用即永远不执行）")
    m = _ENEMY.search(text)
    if m:
        out.facts["enemy"] = m.group(0)
    return out


def point_params(pid: str) -> tuple[str, str]:
    return f"{POINT_PREFIX}{pid}_lat", f"{POINT_PREFIX}{pid}_lon"


def route_param(rid: str) -> str:
    return f"{ROUTE_PREFIX}{rid}"


def interface_parameters(interface: ScenarioInterface) -> dict[str, dict[str, Any]]:
    """Model parameters standing for the interface's points and routes (placeholder values;
    the binding supplies the real ones)."""
    out: dict[str, dict[str, Any]] = {}
    for pid, p in interface.points.items():
        lat, lon = point_params(pid)
        desc = p.description or ANCHOR_TEXT[p.anchor]
        for name, part, word in ((lat, "lat", "纬度"), (lon, "lon", "经度")):
            out[name] = {
                "type": "angle",
                "value": 0.0,
                "unit": "deg",
                "description": f"{desc}（{word}，由想定给出）",
                "scenario": f"point:{pid}:{part}:{p.anchor}",
            }
    for rid, r in interface.routes.items():
        out[route_param(rid)] = {
            "type": "string",
            "value": f"ir_route_{rid}",
            "description": f"路线 {rid}（由想定布设）",
            "scenario": f"route:{rid}:{r.start}:{r.end}",
        }
    return out


# ------------------------------------------------------------------------------ binding
class BoundRoute(_Strict):
    name: str  # route name in the run (AFSIM global route, 2D simulation route)
    plane: list[tuple[float, float]]  # x east, y north (m) from the origin
    geo: list[tuple[float, float]]  # latitude, longitude (deg)
    speed_mps: float = DEFAULT_ROUTE_SPEED_MPS
    arrival_tolerance_m: float = DEFAULT_ARRIVAL_TOLERANCE_M


class Binding(_Strict):
    origin: tuple[float, float]
    heading_deg: float
    enemy_range_m: float
    end_s: float
    points_plane: dict[str, tuple[float, float]] = Field(default_factory=dict)
    points_geo: dict[str, tuple[float, float]] = Field(default_factory=dict)
    routes: dict[str, BoundRoute] = Field(default_factory=dict)
    events: dict[str, float | None] = Field(default_factory=dict)  # id → time (None: never)
    parameters: dict[str, Any] = Field(default_factory=dict)  # model parameter → value
    enemies_plane: list[tuple[float, float]] = Field(default_factory=list)
    # enemy approach (kit ``[enemy] approach_speed_mps``): velocity east / north (m/s), and
    # the distance from the origin where the approach ends (AFSIM route end)
    enemy_velocity: tuple[float, float] = (0.0, 0.0)
    enemy_stop_m: float | None = None
    # when the approach starts and ends (s); before the start the enemy stands still
    enemy_move_start_s: float = 0.0
    enemy_move_stop_s: float | None = None
    defaults: list[str] = Field(default_factory=list)  # values initialised by default

    def route_named(self, name: str) -> BoundRoute | None:
        return next((r for r in self.routes.values() if r.name == name), None)


def _geo(origin: tuple[float, float], east: float, north: float) -> tuple[float, float]:
    lat0, lon0 = origin
    return (
        lat0 + north / 111_320.0,
        lon0 + east / (111_320.0 * math.cos(math.radians(lat0))),
    )


def scenario_tags(model_or_spec: dict[str, Any]) -> dict[str, str]:
    """Model parameter → its scenario tag (normalized model or IR spec)."""
    out = {}
    for name, p in (model_or_spec.get("parameters") or {}).items():
        if isinstance(p, dict) and p.get("scenario"):
            out[name] = str(p["scenario"])
    return out


def interface_of(model_or_spec: dict[str, Any]) -> ScenarioInterface:
    """The points and routes a model's scenario parameters name (anchors by name when the
    model alone is given: post → own_start, other points → ahead)."""
    out = ScenarioInterface()
    for tag in scenario_tags(model_or_spec).values():
        parts = tag.split(":")
        kind, ident = parts[0], parts[1] if len(parts) > 1 else ""
        if kind == "point" and ident not in out.points:
            anchor = parts[3] if len(parts) > 3 and parts[3] in ANCHORS else None
            anchor = anchor or ("own_start" if ident == "post" else "ahead")
            out.points[ident] = InterfacePoint(anchor=anchor)  # type: ignore[arg-type]
        elif kind == "route" and ident not in out.routes:
            start = parts[2] if len(parts) > 2 else "post"
            end = parts[3] if len(parts) > 3 else "ahead"
            out.routes[ident] = InterfaceRoute(start=start, end=end)
    for r in out.routes.values():
        for end, anchor in ((r.start, "own_start"), (r.end, "ahead")):
            out.points.setdefault(end, InterfacePoint(anchor=anchor))  # type: ignore[arg-type]
    return out


def bind(
    interface: ScenarioInterface,
    kit: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
) -> Binding:
    """Concrete values for one scenario: the kit's (local run configuration) where it states
    them, reasonable defaults for everything else."""
    kit = kit or {}
    meta = kit.get("meta") or {}
    enemy = kit.get("enemy") or {}
    bkit = kit.get("binding") or {}
    defaults: list[str] = []
    origin = tuple(float(x) for x in meta.get("origin", DEFAULT_ORIGIN))
    heading = float(meta.get("heading_deg", DEFAULT_HEADING_DEG))
    rng = float(enemy.get("range_m", DEFAULT_ENEMY_RANGE_M))
    end_s = float(meta.get("end_time_s", DEFAULT_END_S))
    if "origin" not in meta:
        defaults.append(f"出发位置取默认 {origin}")
    if "range_m" not in enemy:
        defaults.append(f"敌方距离取默认 {rng:g} m")
    ahead_m = float(bkit.get("ahead_m", meta.get("ahead_m", min(350.0, rng / 2))))
    toward_m = float(bkit.get("toward_enemy_m", rng / 2))
    tol = float(bkit.get("arrival_tolerance_m", DEFAULT_ARRIVAL_TOLERANCE_M))
    speed = float(bkit.get("route_speed_mps", meta.get("route_speed_mps", DEFAULT_ROUTE_SPEED_MPS)))
    h = math.radians(heading)

    def along(d: float) -> tuple[float, float]:
        return d * math.sin(h), d * math.cos(h)  # east, north

    out = Binding(origin=origin, heading_deg=heading, enemy_range_m=rng, end_s=end_s)  # type: ignore[arg-type]
    given_points = bkit.get("points") or {}
    for pid, p in interface.points.items():
        if pid in given_points:
            east, north = (float(x) for x in given_points[pid])
        elif p.anchor == "own_start":
            east, north = 0.0, 0.0
        elif p.anchor == "toward_enemy":
            east, north = along(toward_m)
            defaults.append(f"点 {pid} 取向敌方 {toward_m:g} m")
        else:
            east, north = along(ahead_m)
            defaults.append(f"点 {pid} 取前方 {ahead_m:g} m")
        out.points_plane[pid] = (round(east, 3), round(north, 3))
        out.points_geo[pid] = _geo(origin, east, north)  # type: ignore[arg-type]
        lat, lon = point_params(pid)
        out.parameters[lat], out.parameters[lon] = out.points_geo[pid]
    for rid, r in interface.routes.items():
        a = out.points_plane.get(r.start, (0.0, 0.0))
        b = out.points_plane.get(r.end, along(ahead_m))
        name = f"ir_route_{rid}"
        out.routes[rid] = BoundRoute(
            name=name,
            plane=[a, b],
            geo=[_geo(origin, *a), _geo(origin, *b)],  # type: ignore[list-item]
            speed_mps=speed,
            arrival_tolerance_m=tol,
        )
        out.parameters[route_param(rid)] = name
    events = kit.get("events") or {}
    for eid, ev in interface.events.items():
        if eid in events:
            t = float(events[eid])
            out.events[eid] = end_s + t if t < 0 else t
        elif ev.required:
            out.events[eid] = end_s - 20.0
            defaults.append(f"事件 {eid} 在结束前 20 s 发出")
        else:
            out.events[eid] = None  # an optional order the run does not give
    gap = float(enemy.get("spacing_m", 200.0))
    n = int(enemy.get("count", 2))
    across = h + math.pi / 2
    approach = float(enemy.get("approach_speed_mps", 0.0) or 0.0)
    if approach > 0:
        # the enemy drives towards the own start, along the heading
        out.enemy_velocity = (round(-approach * math.sin(h), 3), round(-approach * math.cos(h), 3))
        out.enemy_stop_m = float(enemy.get("stop_range_m", 500.0))
        out.enemy_move_start_s = max(0.0, float(enemy.get("approach_start_s", 0.0) or 0.0))
        out.enemy_move_stop_s = round(
            out.enemy_move_start_s + max(0.0, rng - out.enemy_stop_m) / approach, 3
        )
    for i in range(n):
        off = (i - (n - 1) / 2) * gap
        out.enemies_plane.append(
            (
                round(rng * math.sin(h) + off * math.sin(across), 3),
                round(rng * math.cos(h) + off * math.cos(across), 3),
            )
        )
    if model is not None:
        # parameters the model tags as scenario values but the interface does not name
        for name, tag in scenario_tags(model).items():
            if name not in out.parameters:
                defaults.append(f"想定参数 {name}（{tag}）没有对应的接口项，保留模型中的值")
    out.defaults = defaults
    return out


def apply(model: dict[str, Any], binding: Binding) -> dict[str, Any]:
    """A copy of a normalized model with the bound values in its scenario parameters."""
    out = copy.deepcopy(model)
    for name, value in binding.parameters.items():
        p = (out.get("parameters") or {}).get(name)
        if isinstance(p, dict):
            p["value"] = value
    return out


def binding_for(
    model: dict[str, Any],
    kit: dict[str, Any] | None = None,
    interface: ScenarioInterface | None = None,
) -> Binding:
    """The binding of a model in one scenario (its interface from the model when not given)."""
    iface = interface or interface_of(model)
    if interface is not None:
        # names the model uses but the given interface lacks still get a value
        extra = interface_of(model)
        for k, v in extra.points.items():
            iface.points.setdefault(k, v)
        for k, v in extra.routes.items():
            iface.routes.setdefault(k, v)
    return bind(iface, kit, model)


def environment(binding: Binding) -> dict[str, Any]:
    """The 2D simulation base environment of a binding."""
    return {
        "origin_deg": list(binding.origin),
        "routes": {r.name: [list(p) for p in r.plane] for r in binding.routes.values()},
    }


# ------------------------------------------------------------------------ binding check
# what an action needs the scenario to supply (capability cards, ``requires``): its route
# comes from the interface (the run lays it out and gives the route action its endpoint
# data), its point too
REQUIRES: dict[str, tuple[str, ...]] = {
    "movement.follow_route": ("route",),
    "movement.move_to_point": ("point",),
}
_PARAM_OF = {"route": ("route_name",), "point": ("latitude_deg", "longitude_deg")}


def _leaves(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "action":
            out.append(node)
        for value in node.values():
            if isinstance(value, dict | list):
                _leaves(value, out)
    elif isinstance(node, list):
        for value in node:
            _leaves(value, out)


# Who fixes an interface-parameter finding (routing; see ``interface_param_issues``):
#   unbound parameter (the kit has no value for a scenario parameter) -> scenario / kit:
#       ``check_binding``; the model is not run until the binding gives it
#   not_interface (a name or literal that is no interface route / point) -> model: repair
#   missing, written by the drafter                                       -> model: repair
#   missing, inside a tactic-set subtree -> the platform kit's default; AFSIM decides
#       (the 2D simulation cannot judge it), the AFSIM loop is not skipped
ROUTING = {
    "unbound": "scenario",
    "not_interface": "model",
    "missing:model": "model",
    "missing:subtree": "kit_default",
}


class ParamIssue(BaseModel):
    """A route / point action whose route / point parameter is absent or does not come from
    the scenario interface."""

    kind: Literal["missing", "not_interface"]
    role: str
    state: str
    action: str
    param: str
    need: str  # route | point
    origin: str  # "model" (written by the drafter) or the tactic-set subtree it came from
    value: str | None = None  # what was written instead (not_interface)
    literal: bool = False  # a written value, not a parameter (also SCN_LITERAL in the sheet)

    @property
    def route(self) -> str:
        """``ROUTING`` key: who fixes it."""
        if self.kind == "not_interface":
            return "not_interface"
        return "missing:model" if self.origin == "model" else "missing:subtree"

    def message(self) -> str:
        kind = "路线" if self.need == "route" else "位置"
        where = f"{self.role}.{self.state}"
        if self.kind == "not_interface":
            return (
                f"{where} 中的 {self.action} 的 {self.param} 是 {self.value!r}，不是想定接口提供的"
                f"{kind}：想定不会布设它，改用 {self.need}=<接口名>"
            )
        if self.origin == "model":
            return (
                f"{where} 中的 {self.action} 没有写{kind}参数，车辆不知道去哪："
                f"写成 {self.need}=<接口名>（见想定接口）"
            )
        return (
            f"{where}：子树 {self.origin} 中的 {self.action} 没有{kind}参数，由平台装备包的"
            "默认值提供（二维仿真判定不了，以 AFSIM 运行为准；该角色若不应沿路线行进，"
            "换用适合该角色的子树）"
        )


def interface_param_issues(model: dict[str, Any]) -> list[ParamIssue]:
    """Route / point actions (``REQUIRES``) whose parameter is absent or is not an interface
    route / point, per role and state (``ROUTING`` says who fixes each)."""
    tags = scenario_tags(model)
    out: list[ParamIssue] = []
    seen: set[tuple[str, ...]] = set()
    for rid, role in (model.get("roles") or {}).items():
        for sname, state in (role.get("states") or {}).items():
            leaves: list[dict[str, Any]] = []
            _leaves(state, leaves)
            for leaf in leaves:
                action = str(leaf.get("action"))
                origin = str(leaf.get("origin") or "model")
                params = leaf.get("params") or {}
                for need in REQUIRES.get(action, ()):
                    absent = [p for p in _PARAM_OF[need] if params.get(p) is None]
                    issue = None
                    if absent:
                        issue = ParamIssue(
                            kind="missing",
                            role=rid,
                            state=str(sname),
                            action=action,
                            param=absent[0],
                            need=need,
                            origin=origin,
                        )
                    else:
                        for pname in _PARAM_OF[need]:
                            value = params[pname]
                            ref = value.get("parameter_ref") if isinstance(value, dict) else None
                            if ref not in tags:
                                issue = ParamIssue(
                                    kind="not_interface",
                                    role=rid,
                                    state=str(sname),
                                    action=action,
                                    param=pname,
                                    need=need,
                                    origin=origin,
                                    value=str(ref or value),
                                    literal=ref is None,
                                )
                                break
                    if issue is None:
                        continue
                    key = (issue.kind, rid, str(sname), action, need, origin)
                    if key not in seen:
                        seen.add(key)
                        out.append(issue)
    return out


def check_binding(model: dict[str, Any], binding: Binding) -> list[str]:
    """What the scenario cannot give the model (a normalized model): a scenario parameter the
    binding has no value for. Route / point actions that name no interface route / point, or
    none at all, are the model's (or the kit's) business: ``interface_param_issues``."""
    out: list[str] = []
    for name, tag in scenario_tags(model).items():
        if name not in binding.parameters:
            out.append(
                f"想定参数 {name}（{tag}）在想定绑定中没有取值：在想定设置的 [binding] 中给出"
            )
    return out


def bind_scenario(raw: dict[str, Any], model: dict[str, Any], binding: Binding) -> dict[str, Any]:
    """A 2D simulation scenario laid out from the binding: origin and routes always from the
    binding; vehicles and hostiles from the binding unless the scenario writes them (a
    hostile that appears, disappears or moves is a variation the scenario may write)."""
    sc = copy.deepcopy(raw)
    env = dict(sc.get("environment") or {})
    env["origin_deg"] = list(binding.origin)
    routes = dict(env.get("routes") or {})
    routes.update(environment(binding)["routes"])
    env["routes"] = routes
    env.setdefault("duration_s", binding.end_s + 40.0)
    sc["environment"] = env
    vehicles = dict(sc.get("vehicles") or {})
    roles = model.get("roles") or {}
    h = math.radians(binding.heading_deg)
    across = h + math.pi / 2
    total = sum(int((r.get("role") or {}).get("count", 1)) for r in roles.values())
    k = 0
    for rid, role in roles.items():
        n = int((role.get("role") or {}).get("count", 1))
        if rid not in vehicles:
            specs = []
            for _ in range(n):
                off = (k - (total - 1) / 2) * 50.0
                specs.append(
                    {
                        "position_m": [
                            round(off * math.sin(across), 3),
                            round(off * math.cos(across), 3),
                        ],
                        "heading_deg": binding.heading_deg,
                        "max_speed_mps": 10.0,
                    }
                )
                k += 1
            vehicles[rid] = specs
        else:
            k += n
    sc["vehicles"] = vehicles
    if not sc.get("hostiles"):
        sc["hostiles"] = [
            {"id": f"e{i + 1}", "position_m": list(p)}
            | (
                {
                    "velocity_mps": list(binding.enemy_velocity),
                    "move_start_s": binding.enemy_move_start_s,
                    "move_stop_s": binding.enemy_move_stop_s,
                }
                if any(binding.enemy_velocity)
                else {}
            )
            for i, p in enumerate(binding.enemies_plane)
        ]
    return sc


def summary(binding: Binding) -> str:
    """The binding as the drafter of 2D test scenarios sees it."""
    lines = [
        "出发位置为平面原点 (0, 0)，x 向东、y 向北（米）；车辆按角色排在出发位置",
        "敌方基准位置（hostiles 省略时使用）："
        + "；".join(f"e{i + 1} ({p[0]:g}, {p[1]:g})" for i, p in enumerate(binding.enemies_plane)),
    ]
    for pid, p in binding.points_plane.items():
        lines.append(f"位置 {pid}：({p[0]:g}, {p[1]:g})")
    for rid, r in binding.routes.items():
        lines.append(f"路线 {rid}：" + " → ".join(f"({x:g}, {y:g})" for x, y in r.plane))
    return "\n".join(f"- {line}" for line in lines)
