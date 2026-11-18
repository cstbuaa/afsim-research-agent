"""Drafting from a verified skeleton (design ``skeleton-drafting-v1``).

The model does not write roles, state diagrams, events or parameters. It answers one
structured choice: which skeleton (``training.pilot_fp``: K1–K5 three-vehicle formation, K6
one observer), which modules, the numbers the requirement states, how the requirement's
roles map onto the skeleton's, which states implement each clause, and which clauses the
skeleton cannot cover. The program then writes the whole sheet (state diagrams, trees,
conditions, events, L2 tests and 2D scenarios) from the skeleton generator, whose output
passes the offline checks by construction. Clauses left uncovered go to the repair loop as
small edits; a requirement no skeleton fits falls back to drafting from scratch.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from afsim_research_agent.training.pilot_fp import Spec, roles, sheet


class Uncovered(BaseModel):
    clause: str
    why: str = ""


class SkeletonChoice(BaseModel):
    """The model's whole answer for a skeleton draft."""

    applicable: bool = Field(
        description="有骨架能实现需求的主体时为 true；否则 false，退回从零起草"
    )
    reason: str = Field(default="", description="一句话：为什么选这个骨架，或为什么都不适用")
    family: Literal["formation", "strike", "reform"] = Field(
        default="formation",
        description="formation：编队搜索跟踪/单车观察骨架；strike：排长集中分配、成员车交战骨架；"
        "reform：排长依次下达队形、僚车按槽位跟随的队形变换骨架",
    )
    formations: list[
        Literal[
            "column", "line_abreast", "wedge_forward", "wedge_rear", "echelon_left", "echelon_right"
        ]
    ] = Field(default_factory=list, description="reform：按需求顺序列出要排的队形")
    hold_s: float | None = Field(default=None, description="reform：每种队形保持多少秒后变换下一个")
    spacing_m: float | None = Field(
        default=None, description="reform：纵向车间距（米），需求给出才填"
    )
    lateral_m: float | None = Field(
        default=None, description="reform：横向车间距（米），需求给出才填"
    )
    reattack: bool = Field(
        default=True,
        description="strike：目标未摧毁且开火未达上限时再次攻击；false 则每个任务只打一轮",
    )
    engage_limit_s: float | None = Field(
        default=None, description="strike：交战时限（秒），到时排长停止分配并停火；需求没说就不填"
    )
    cease_order: str | None = Field(
        default=None, description="strike：上级停火命令对应的外部事件（取自想定事件）；没有就不填"
    )
    destroy: bool = Field(
        default=False,
        description="strike：需求要求把敌方打掉（摧毁、歼灭、直至被摧毁）时为 true，验收时检查敌方全部被摧毁；"
        "只说开火、射击时为 false",
    )
    formation: bool = Field(
        default=True, description="三车编队（长车 1 + 僚车 2）为 true；单车定点观察为 false"
    )
    assemble: bool = Field(default=False, description="出发前先集结成纵队")
    wait_start: bool = Field(default=False, description="先等上级开始命令（外部事件 start_order）")
    search_delay_s: float | None = Field(
        default=None, description="前出多少秒后才开始搜索；需求没说就不填"
    )
    lost_hold: bool = Field(default=False, description="目标丢失后原地保持、尝试重新获取")
    on_timeout: Literal["resume", "shutdown"] = Field(
        default="resume", description="重新获取超时后：恢复纵队搜索 resume / 关机 shutdown"
    )
    reacquire_timeout_s: float | None = Field(default=None, description="重新获取时限（秒）")
    withdraw: Literal["none", "keep", "stop"] = Field(
        default="none",
        description="敌方逼近：none 不处理；keep 边退边保持距离；stop 距敌小于阈值即停车关机（撤离）",
    )
    withdraw_threshold_m: float | None = Field(
        default=None, description="withdraw=stop 时的撤离距离阈值（米）"
    )
    desired_range_m: float | None = Field(default=None, description="保持距离（米）")
    search_limit_s: float | None = Field(
        default=None, description="限时搜索：超过多少秒没发现就关机"
    )
    share_target: bool = Field(default=False, description="长车在展开和跟踪时广播共享目标航迹")
    stop_events: list[str] = Field(
        default_factory=list,
        description="除 cancelled 外，收到后也转去安全关机的外部事件（取自想定事件）",
    )
    wing_offset_m: float | None = Field(default=None, description="横队侧翼偏移（米）")
    column_spacing_m: float | None = Field(default=None, description="纵队间距（米）")
    leader_role: str = Field(default="", description="需求里长车（或单车观察车）的角色名")
    member_role: str = Field(default="", description="需求里僚车的角色名")
    implements: dict[str, list[str]] = Field(
        default_factory=dict, description="条款编号 → 实现它的状态（角色.状态）"
    )
    uncovered: list[Uncovered] = Field(
        default_factory=list, description="骨架实现不了的条款（之后由修复做小修改）"
    )


CATALOG = """可选的骨架（已验证；状态、树、条件、事件、测试和想定由程序生成，你不写）：

三车编队（formation=true；长车 1 辆执行编队指挥动作，僚车 2 辆只做本车感知，站位由长车带动）
长车状态（按顺序，括号里是开关）：
  WAIT_START（wait_start：等上级开始命令 start_order）
  ASSEMBLE（assemble：集结成纵队，纵队到位后出发）
  ADVANCE（search_delay_s：先沿路线前出，到时后才开搜索）
  SEARCH_ADVANCE：沿想定路线 approach 纵队前进并搜索；发现目标 → CONTACT_DEPLOY
    （search_limit_s：限时搜索，到时未发现 → SHUTDOWN）
  CONTACT_DEPLOY：停车，僚车展开成面向目标的横队；横队到位 → STANDOFF_TRACK
  STANDOFF_TRACK：长车与目标保持距离 desired_range_m 持续跟踪（这一步会自己前进或后退到保持距离）
    目标丢失 → TARGET_LOST_HOLD（lost_hold）或 SHUTDOWN（没有 lost_hold）
    withdraw=keep：敌方逼近时边退边保持距离（保持距离那一步自己做，不另设状态）
    withdraw=stop：距敌小于 withdraw_threshold_m → SHUTDOWN（撤离：停车、关传感器）
  TARGET_LOST_HOLD（lost_hold）：原地保持并重新获取；重新发现 → STANDOFF_TRACK；
    超时 reacquire_timeout_s → RESUME_SEARCH（on_timeout=resume，恢复纵队后回到 SEARCH_ADVANCE）或 SHUTDOWN
  SHUTDOWN：安全关机（停车、关搜索雷达）→ COMPLETE
  任何状态收到 cancelled（及 stop_events 里的事件）→ SHUTDOWN
  share_target：长车在 CONTACT_DEPLOY、STANDOFF_TRACK 中同时广播共享目标航迹
僚车状态：SEARCH → TRACK →（lost_hold 时 REACQUIRE）→ SHUTDOWN → COMPLETE；只做感知

单车定点观察（formation=false）：原地 SEARCH → TRACK → REACQUIRE（超时回 SEARCH）→ SHUTDOWN → COMPLETE

排长集中分配 + 成员车交战（family=strike；排长车 1 辆，成员车 2 辆）
排长车：（wait_start：WAIT_START 等开始命令）→ PREPARE 开雷达 → ALLOCATING：每拍完成一次
  收集目标、评估威胁、检查成员车、筛选、建候选、一车一目标分配、电台下发（任务 3 s 有效，每拍重发）
  （engage_limit_s：交战时限到 → HOLD 停止分配、停火待命，直到任务结束）→ CEASE 停火、关雷达 → COMPLETE
成员车：PREPARE 开雷达 → WAITING_TASK（停火待命，清过期任务）→ 收到任务 → VALIDATING（采纳任务、
  校验有效、武器状态、射线友军检查）→ SELECTING（筛选、打分、选定武器）→ FIRING（瞄准、开火）→
  OBSERVING（观察毁伤；reattack：未摧毁且本任务开火不到 3 次 → VALIDATING 再攻击，否则 → WAITING_TASK）
  每一步失败（任务过期、射线上有友车、射程外、未装填、开火上限）→ WAITING_TASK 停火等待新任务
  CEASE 停火、关雷达 → COMPLETE
destroy：需求要求打掉敌方时，验收检查敌方全部被摧毁

排长下达队形、僚车按槽位跟随的队形变换（family=reform；排长车 1 辆，僚车 2 辆，原地变换）
排长车：INIT 编制初始化 → 按 formations 顺序每种队形一个状态（F_COLUMN 纵队、F_LINE 横队、
  F_WEDGE_FWD 楔形（僚车在后两侧）、F_WEDGE_REAR 倒楔形、F_ECH_LEFT 左梯队、F_ECH_RIGHT 右梯队），
  每种保持 hold_s 秒后变换下一个；最后一种保持到任务结束 → SHUTDOWN → COMPLETE
僚车：INIT → FOLLOW（持续驶向并保持分配的槽位）→ SHUTDOWN → COMPLETE
验收按 AFSIM 位置检查每种队形都排成（spacing_m / lateral_m 间距）
任何状态收到 cancelled → CEASE；收到 cease_order 停火命令 → HOLD（全部停火待命，cancelled 后再收尾）
"""


def prompt(
    requirement: str,
    clauses: list[dict[str, Any]],
    roles_hint: dict[str, Any],
    events: dict[str, str],
) -> str:
    cl = "\n".join(f"- {c['id']}：{c['text']}" for c in clauses)
    rl = "\n".join(
        f"- {k}：{v.get('count', 1)} 辆；{v.get('description') or ''}"
        for k, v in roles_hint.items()
    )
    ev = "\n".join(f"- {k}：{v}" for k, v in events.items()) or "（无）"
    return f"""你在为装甲车战术需求选一个已验证的骨架。不要写状态图、树或参数代码：只按下面的格式回答一份选择。

需求：
{requirement}

需求条款（implements 和 uncovered 用这些编号）：
{cl}

需求给出的角色：
{rl or "（未给出）"}

想定提供的外部事件：
{ev}

{CATALOG}
回答要求：
- 先定 family：编队搜索/跟踪/观察类选 formation；排长分配目标、成员车开火交战类选 strike；原地按顺序变换队形（纵队、横队、楔形、梯队）选 reform。
- 选能覆盖需求主体（阶段顺序、队形、发现/丢失/撤离的处理）的骨架和开关；需求没提到的开关保持默认（关），不要为了"更完整"打开需求没要求的开关（如广播共享航迹）。
- 数值只填需求明确给出的（如"保持 3 km"填 desired_range_m=3000，"约 10 秒后开始搜索"填 search_delay_s=10）；没给的不填，程序用默认值并标为待复核。
- leader_role / member_role 填需求角色里的名字；需求把三辆车写成三个各 1 辆的角色时，长车那个填 leader_role，另两个里取一个填 member_role，并在 reason 里说明。
- implements：每条条款写出实现它的状态，格式"角色名.状态"。角色名只能用 leader_role / member_role 的值（就是需求角色里的名字），不要用战术卡里的角色名（如 lead_vehicle、wingman_A）；状态只能是上面列出的、并且由所选开关产生的状态。
- 骨架做不到的条款（如梯队楔形队形、监视交接、单车隐蔽撤离）放进 uncovered，写清原因；不要为它们改骨架。
- 需求主体（阶段顺序本身）与所有骨架都不符时 applicable=false。
"""


def to_spec(choice: SkeletonChoice, events: dict[str, str]) -> Spec:
    """The skeleton generator's spec for a choice (the K label follows from the modules)."""
    if not choice.formation:
        label = "K6"
    elif choice.withdraw == "keep":
        label = "K4"
    elif choice.search_limit_s:
        label = "K5"
    elif choice.assemble:
        label = "K3"
    elif choice.lost_hold:
        label = "K2"
    else:
        label = "K1"
    s = Spec(skeleton=label, seed=0)
    s.assemble = choice.assemble and choice.formation
    s.wait_start = choice.wait_start
    s.lost_hold = choice.lost_hold or not choice.formation
    s.on_timeout = choice.on_timeout if choice.formation else "resume"
    s.withdraw = None if choice.withdraw == "none" or not choice.formation else choice.withdraw
    s.search_limit_s = choice.search_limit_s or None
    s.search_delay_s = choice.search_delay_s or None if choice.formation else None
    s.share_target = choice.share_target and choice.formation
    s.stop_events = [e for e in choice.stop_events if e in events and e != "cancelled"]
    s.event_text = {k: v for k, v in events.items()}
    stated: list[str] = []
    for key in ("desired_range_m", "reacquire_timeout_s", "wing_offset_m", "column_spacing_m"):
        v = getattr(choice, key)
        if v:
            setattr(s, key, float(v))
            stated.append(key)
    if s.withdraw == "stop":
        thr = choice.withdraw_threshold_m or s.desired_range_m * 0.6
        s.withdraw_threshold_m = float(min(thr, s.desired_range_m - s.range_tolerance_m - 100))
        if choice.withdraw_threshold_m:
            stated.append("withdraw_threshold_m")
    if s.search_limit_s:
        stated.append("search_limit_s")
    s.stated = sorted(stated)
    names: dict[str, str] = {}
    if choice.leader_role:
        names["leader" if choice.formation else "observer"] = choice.leader_role
    if choice.member_role and choice.formation:
        names["member"] = choice.member_role
    s.names = names
    return s


def build(
    choice: SkeletonChoice,
    *,
    events: dict[str, str],
    display_name: str = "",
    intent: str = "",
    clauses: list[str] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """The tactic sheet for a choice, notes (dropped implements, defaults used) and the
    skeleton generator's own request (its tests and offline checks use it)."""
    if choice.family == "strike":
        return _build_strike(choice, events=events, display_name=display_name,
                             intent=intent, clauses=clauses)  # fmt: skip
    if choice.family == "reform":
        return _build_reform(choice, events=events, display_name=display_name,
                             intent=intent, clauses=clauses)  # fmt: skip
    s = to_spec(choice, events)
    notes: list[str] = []
    states = {r: set(m.states) for r, (_, m, _) in roles(s).items()}
    lead = s.name("leader" if s.formation else "observer")
    implements = _implements(choice, states, lead, s.name("member"), clauses, notes)
    params: dict[str, Any] = {}
    defaults = {
        "desired_range_m": ("m", "保持距离"),
        "reacquire_timeout_s": ("s", "重新获取时限"),
    }
    for key, (unit, what) in defaults.items():
        if key == "reacquire_timeout_s" and not s.lost_hold:
            continue
        if key == "desired_range_m" and not s.formation:
            continue
        if key not in s.stated:
            notes.append(f"需求没有给出{what}，用骨架默认值 {getattr(s, key):g} {unit}（待复核）")
    if s.withdraw == "stop" and "withdraw_threshold_m" in s.stated:
        params["withdraw_threshold_m"] = {
            "value": f"{s.withdraw_threshold_m:g} m",
            "basis": "user_specified",
            "rationale": "距敌小于该距离即撤离",
        }
    extra: dict[str, Any] = {
        "model": {
            "display_name": display_name or f"骨架 {s.skeleton}",
            "intent": intent or choice.reason or "按已验证骨架生成",
        },
        "implements": implements or None,
        "assumptions": [f"按已验证骨架 {s.skeleton} 生成（{choice.reason}）"]
        if choice.reason
        else None,
    }
    if params:
        extra["parameters"] = params
    text, request = sheet(s, extra)
    text = re.sub(
        r"^# 战术稿：.*$", f"# 战术稿：{display_name or s.model_id}", text, count=1, flags=re.M
    )
    for u in choice.uncovered:
        notes.append(f"骨架未覆盖的条款 {u.clause}：{u.why}")
    return text, notes, request


LEADISH = re.compile(r"lead|leader|command|cmd|hq|chief|长车|排长|主|指挥", re.I)


def _implements(
    choice: SkeletonChoice,
    states: dict[str, set[str]],
    lead: str,
    member: str,
    clauses: list[str] | None,
    notes: list[str],
) -> dict[str, list[str]]:
    """Clause → states, checked against the skeleton. A role name the skeleton does not
    know (a tactic card's own, such as lead_vehicle / wingman_A) maps onto the leader when
    it reads like one, else onto the member."""
    out: dict[str, list[str]] = {}
    known = set(clauses or []) or None
    for cid, refs in choice.implements.items():
        if known is not None and cid not in known:
            notes.append(f"implements 里的条款 {cid} 不在需求条款中，已去掉")
            continue
        good: list[str] = []
        for ref in refs:
            role, _, st = str(ref).partition(".")
            if role not in states:
                mapped = lead if LEADISH.search(role) else member
                if mapped in states and st in states[mapped]:
                    notes.append(f"条款 {cid} 的角色 {role} 按 {mapped} 处理")
                    role = mapped
            if st in states.get(role, set()):
                if f"{role}.{st}" not in good:
                    good.append(f"{role}.{st}")
            else:
                notes.append(f"条款 {cid} 的 {ref} 不是骨架里的状态，已去掉")
        if good:
            out[cid] = good
    return out


def _build_strike(
    choice: SkeletonChoice,
    *,
    events: dict[str, str],
    display_name: str,
    intent: str,
    clauses: list[str] | None,
) -> tuple[str, list[str], dict[str, Any]]:
    from afsim_research_agent.training import strike_fp as SF

    names = {}
    if choice.leader_role:
        names["leader"] = choice.leader_role
    if choice.member_role:
        names["member"] = choice.member_role
    order = choice.cease_order if choice.cease_order in events else None
    spec = SF.StrikeSpec(
        reattack=choice.reattack,
        wait_start=choice.wait_start and "start_order" in events,
        engage_limit_s=choice.engage_limit_s or None,
        cease_order=order if order != "cancelled" else None,
        destroy=choice.destroy,
        names=names,
        event_text=dict(events),
    )
    notes: list[str] = []
    if choice.cease_order and spec.cease_order is None:
        notes.append(f"想定不会发出 {choice.cease_order}：骨架不加停火命令")
    rl = SF.roles(spec)
    states = {r: set(m.states) for r, (_, m, _) in rl.items()}
    implements = _implements(
        choice, states, spec.name("leader"), spec.name("member"), clauses, notes
    )
    extra: dict[str, Any] = {
        "model": {
            "display_name": display_name or "打击骨架",
            "intent": intent or choice.reason or "按已验证的打击骨架生成",
        },
        "implements": implements or None,
        "assumptions": [f"按已验证的打击骨架生成（{choice.reason}）"] if choice.reason else None,
    }
    text, request = SF.sheet(spec, extra)
    text = re.sub(
        r"^# 战术稿：.*$", f"# 战术稿：{display_name or spec.model_id}", text, count=1, flags=re.M
    )
    for u in choice.uncovered:
        notes.append(f"骨架未覆盖的条款 {u.clause}：{u.why}")
    return text, notes, request


def _build_reform(
    choice: SkeletonChoice,
    *,
    events: dict[str, str],
    display_name: str,
    intent: str,
    clauses: list[str] | None,
) -> tuple[str, list[str], dict[str, Any]]:
    from afsim_research_agent.training import reform_fp as RF

    notes: list[str] = []
    names = {}
    if choice.leader_role:
        names["leader"] = choice.leader_role
    if choice.member_role:
        names["member"] = choice.member_role
    forms = list(choice.formations) or ["column", "line_abreast"]
    if not choice.formations:
        notes.append("需求没列出队形：按 纵队→横队 起草，待复核")
    spec = RF.ReformSpec(
        formations=forms,
        hold_s=float(choice.hold_s or 60.0),
        spacing_m=float(choice.spacing_m or 50.0),
        lateral_m=float(choice.lateral_m) if choice.lateral_m else None,
        names=names,
        event_text=dict(events),
    )
    for key, label, default in (
        ("hold_s", "每种队形保持时间", "60 s"),
        ("spacing_m", "车间距", "50 m"),
    ):
        if not getattr(choice, key):
            notes.append(f"{label}需求没给，取默认 {default}，待复核")
    states = {
        spec.name("leader"): set(RF.leader_states(spec)),
        spec.name("member"): set(RF.member_states(spec)),
    }
    implements = _implements(
        choice, states, spec.name("leader"), spec.name("member"), clauses, notes
    )
    extra: dict[str, Any] = {
        "model": {
            "display_name": display_name or "队形变换",
            "intent": intent or choice.reason or "按已验证的队形变换骨架生成",
        },
        "implements": implements or None,
        "assumptions": [f"按已验证的队形变换骨架生成（{choice.reason}）"]
        if choice.reason
        else None,
    }
    text, request = RF.sheet(spec, extra)
    if display_name:
        text = re.sub(r"^# 战术稿：.*$", f"# 战术稿：{display_name}", text, count=1, flags=re.M)
    for u in choice.uncovered:
        notes.append(f"骨架未覆盖的条款 {u.clause}：{u.why}")
    return text, notes, request


def describe(choice: SkeletonChoice) -> str:
    data = choice.model_dump(exclude_defaults=True)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
