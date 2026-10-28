"""Prompt templates (Chinese) for every Falcon stage.

Prompts carry only what the stage needs: the requirement, the relevant
catalog slice, the relevant claims and the work done so far. Raw model
responses are never stored; the trace keeps a hash of each prompt.
"""

from __future__ import annotations

import json
from typing import Any

from afsim_research_agent.capability.constraints import VocabularyConstraints
from afsim_research_agent.ir.catalog import Catalog

PROMPT_VERSION = "falcon-prompts-2026.10.8"

TOPICS = (
    "formation（队形选择）、spacing（间距与几何）、transition（状态/队形转换条件）、"
    "contact（接敌与报告）、bounding（交替跃进/掩护）、communication（通信与同步）、"
    "speed（速度）、terrain（地形）、sensing（探测）、other"
)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- F1


def extraction_prompt(requirement: str, questions: list[str], excerpts: list[dict]) -> str:
    blocks = "\n\n".join(
        f"[{e['excerpt_id']}]（来源 {e['source_id']}，位置 {e['location']}）\n{e['excerpt']}"
        for e in excerpts
    )
    question_lines = "\n".join(f"- {q}" for q in questions) or "- （无）"
    return f"""你是战术资料分析员。从下面的资料片段中抽取可用于战术行为建模的主张。

规则：
1. 每条主张用一句中文陈述片段**明确写出**的内容；不要推断，不要补充片段里没有的数值。
2. quote 必须逐字摘自片段原文（保持原文语言和数字写法），8–300 个字符，包含支撑主张的关键词和全部数字。
3. 主张含数值（距离、速度、时间、角度、比例）时填写 quantity：name 用英文蛇形命名（如 vehicle_interval），
   min/max 为原文数字（单值时两者相等），unit 用原文单位或标准符号（m、km、km/h、kt、mph、s、min、h、deg、percent）。
   原文没有用阿拉伯数字给出的数值，不要填 quantity。
4. topic 取值：{TOPICS}。subject 写主体（如"编队长机""侦察车""防空阵地"），condition 写适用条件（如"开阔地形""夜间"），没有则留空。
5. 与需求无关的片段输出空的 claims；每个片段都要在 items 中出现一次，excerpt_id 原样照抄。

需求：
{requirement}

研究问题：
{question_lines}

资料片段：
{blocks}
"""


def grouping_prompt(topic: str, items: list[dict]) -> str:
    lines = "\n".join(_json(item) for item in items)
    return f"""下面是同一主题（{topic}）下从不同资料中抽取的主张。请把表达**同一命题**（同一主体、同一条件、同一件事）的主张归为一组。

规则：
1. 每个 evidence_id 必须且只能出现在一个组的 members 中；不确定时单独成组。
2. 每组给出一句中文 statement，概括该命题（不添加新数值）。
3. 若两条主张对同一命题给出相互矛盾的说法（数值不同且不能同时成立，或结论相反），在 conflicts 中列出这一对 a、b，并用一句话说明理由。
   仅仅是范围宽窄不同、或适用条件不同，不算冲突。

主张（每行一个 JSON）：
{lines}
"""


# --------------------------------------------------------------------------- catalog rendering


def _mark(constraints: VocabularyConstraints | None, name: str) -> str | None:
    """None: leave the element out; otherwise a (possibly empty) suffix."""
    return "" if constraints is None else constraints.mark(name)


def catalog_actions(catalog: Catalog, constraints: VocabularyConstraints | None = None) -> str:
    lines = []
    for action in catalog.actions.values():
        mark = _mark(constraints, action.name)
        if mark is None:
            continue
        args = ", ".join(
            f"{a.name}:{a.type}{'' if not a.required else '!'}" for a in action.arguments.values()
        )
        flags = []
        if action.every_tick:
            flags.append("可每周期")
        if action.every_tick_forbidden:
            flags.append("禁止每周期")
        if not action.external:
            flags.append("内部")
        extra = f"（{'，'.join(flags)}）" if flags else ""
        req = f" 需要部件:{'/'.join(action.requires)}" if action.requires else ""
        lines.append(f"- {action.name}({args}){req}{extra}{mark}")
    return "\n".join(lines)


def catalog_operands(catalog: Catalog, constraints: VocabularyConstraints | None = None) -> str:
    lines = []
    for op in catalog.operands.values():
        props = []
        for p in op.properties.values():
            mark = _mark(constraints, f"{op.name}.{p.name}")
            if mark is None:
                continue
            arg = f"[参数 {p.argument}]" if p.argument else ""
            props.append(f"{p.name}:{p.type}{arg}{mark}")
        if not props:
            continue
        comp = f"（引用部件 {op.component}）" if op.component else ""
        lines.append(f"- {op.name}{comp}: {', '.join(props)}")
    funcs = []
    for f in catalog.functions.values():
        mark = _mark(constraints, f.name)
        if mark is None:
            continue
        args = ", ".join(f"{k}:{v}" for k, v in f.arguments.items())
        funcs.append(f"{f.name}({args})->{f.returns}{mark}")
    return "\n".join(lines) + f"\n- 函数：{', '.join(funcs)}"


def catalog_patterns(catalog: Catalog) -> str:
    lines = []
    for p in catalog.patterns.values():
        gen = "; ".join(f"{k}: {', '.join(v)}" for k, v in p.generated.items() if v)
        lines.append(
            f"- {p.name}：{p.description} 必填 {list(p.required)}，可选 {list(p.optional)}；"
            f"展开生成（ID 前缀 <协同ID>__）{gen}"
        )
    return "\n".join(lines)


EXPRESSION_GUIDE = """表达式 DSL（JSON 映射，禁止脚本）：
- 逻辑：{"all": [e1, e2]}、{"any": [...]}、{"not": e}、{"condition_ref": "<条件ID>"}、{"literal": true}
- 比较：{"compare": {"left": 操作数, "operator": "lt|lte|gt|gte|eq|ne", "right": 操作数}}
- 操作数：{"parameter_ref": "<参数名>"}、{"literal": 值}、{"blackboard_value": "<黑板ID>.<变量>"}、
  {"track_value": {"property": "nearest_hostile_range"}}、{"terrain_value": {"property": "distance_to_class", "class": "urban"}}、
  {"terrain_value": "class"}、{"mover_value": "available"}、{"self_value": "speed"}、
  {"message_value": {"message": "<消息ID>", "field": "<字段>"}}、{"function_call": {"name": "<函数>", "arguments": {...}}}
- 数值阈值必须引用参数（parameter_ref），不要在条件里写裸数字（0、1 除外）。
"""


# --------------------------------------------------------------------------- F2 / F3


def requirements_prompt(requirement: str, platforms: list[dict], feedback: str = "") -> str:
    plat = _json(platforms) if platforms else "（未指定）"
    return f"""把下面的战术需求拆成可建模的条款，输出 JSON。

规则：
1. text 必须是需求原文中的连续片段（逐字照抄），每个条款只表达一件事；整个需求的每个意思都要被某个条款覆盖。
2. kind 取：behavior（行为/动作）、condition（触发条件）、formation（队形）、coordination（多平台协同）、
   constraint（约束，如保持安全距离）、parameter（单纯的参数值）、out_of_scope。
3. out_of_scope 只用于行为模型根本无法表达的内容（如政治目的、人员士气），并在 reason 写原因。
   **需求没有给出具体数值不是 out_of_scope 的理由**：例如"保持安全距离"应为 constraint，数值由后续参数（依据未知时待确认）表达。
4. user_values：只列出 text 中用户用阿拉伯数字直接给出的数值（name 用英文蛇形命名，unit 用 m、km、km/h、s、min 等；无单位的计数 unit 留空）。
5. roles：条款涉及的角色提示（英文蛇形，如 lead、escort、scout、ground_station），没有则留空。

平台提示：{plat}

需求：
{requirement}
{feedback}"""


PATTERN_GUIDE = """协同模式的典型用途（按需选择，可组合多个，每个模式单独一项）：
- 长机决定队形、僚机按队形保持站位，或长机切换状态时僚机跟随切换 → leader_follower
- 接敌后交替跃进、一组移动一组掩护 → bounding_overwatch（可设 standoff_distance 作为与敌保持的距离）
- 发现情况后向上级报告并等待确认 → report_and_wait（reporter、receiver 都必须是骨架中的角色：排内报告写
  僚车 → 排长；需求要求向排外上级报告时，要么在骨架中加上该角色，要么由排长向僚车通报，不要引用骨架外的单位）
- 多个角色需要同时切换状态，但没有队形跟随 → synchronized_transition"""


def skeleton_prompt(
    requirement: str,
    clauses: list[dict],
    platforms: list[dict],
    terrain_classes: list[str],
    catalog: Catalog,
    intents: list[str],
    concepts: list[str],
    claims: list[dict],
    capabilities: list[str] | None,
    feedback: str = "",
    tactic: str = "",
) -> str:
    caps = (
        f"\n目标引擎支持的能力（角色依赖的 capability 只能取这些，平台类型写作 platform:<类别>）：{_json(capabilities)}"
        if capabilities
        else ""
    )
    return f"""为战术需求设计行为模型骨架（中立行为模型 IR，后续分段填充细节），输出 JSON。

规则：
1. 多平台需求建模为多个角色（roles）；同类平台用一个角色加 count 表示（如 3 辆僚车 = 一个 wingman 角色，count 3）。
   角色 id、状态 id、树 id 用英文蛇形命名，不能含 "__"。
2. 每个角色有自己的状态机；每个状态指定一棵行为树（tree，可多个状态共用）；initial_state 必须是其中一个状态。
   使用 leader_follower 或 synchronized_transition 时，状态表示**全排所处的阶段**，长机与跟随角色的阶段状态使用相同的 id
   （如都有 travel_open、travel_urban、contact），各角色在同一状态下引用各自的树（如 leader_travel_open_tree、wingman_travel_open_tree）；
   长机切换阶段时，协同模式会让跟随角色切换到同名状态。只属于某个角色的状态（如等待确认）可以单独存在。
3. 协同只能从目录模式中选；每个协同一项，只选一个 pattern。消息清单只列协同模式之外的自定义消息（通常为空）。
4. 每个角色、状态、编队、协同、消息都在 implements 中列出它实现的条款 id；所有非 out_of_scope 条款都必须被覆盖。
5. intent 从意图词表选；domain_concepts 从概念词表选（可为空）。
6. 角色依赖：mover_capability（如 route_following_ground）、sensors/comms/weapons 的 id 与 capability；
   需要黑板变量时在 blackboard_variables 中声明（黑板 ID 固定为 mission）。
7. 架构默认 state_machine_with_behavior_trees，决策周期 update_interval_s 默认 1。

{PATTERN_GUIDE}

协同模式目录：
{catalog_patterns(catalog)}

意图词表：{_json(intents)}
概念词表：{_json(concepts)}
地形类别：{_json(terrain_classes)}
平台提示：{_json(platforms) if platforms else "（未指定）"}{caps}

需求：
{requirement}
{tactic}
条款：
{_json(clauses)}

相关资料主张（供理解战术，不要在骨架中写数值）：
{_json(claims)}
{feedback}"""


# --------------------------------------------------------------------------- F4

BASIS_RULES = """basis 规则：status 取 explicit（所引证据直接给出该值或规则，evidence 至少 1 条）、inferred（由证据或假设推断，必须写 rationale，
并引用证据或假设）、conflicting（证据互相矛盾，至少 2 条证据并说明取值理由）、unknown（找不到依据）、
user_specified（**仅当需求原文逐字给出该值或该规则**；否则会被代码降为 unknown）。evidence 只能用列出的 evidence_id，不得编造。"""

NEW_PARAMETERS = """如果本段需要的数值参数（阈值、速度、时间、距离）在"可引用的 ID"中不存在，在 new_parameters 中补充定义
（字段同参数段：name、type、value、unit、description、basis），不要引用未定义的参数。"""

SECTION_GOALS: dict[str, str] = {
    "parameters": f"""生成参数与假设。
- 每个影响战术语义的数值（间距、距离阈值、速度、时间、容差）都是一个参数；参数名英文蛇形。后续段会引用这些参数，尽量一次定义齐全
  （典型：队形间距、站位容差、转换距离阈值、接敌距离、行进速度、跃进距离、与敌保持距离、接敌保持时间）。
- 物理量 type 取 distance/speed/time/angle/altitude/ratio，必须写 unit；整数/布尔/字符串用 integer/boolean/string 且不写 unit。
- 找不到依据的数值也要给出合理占位 value，basis.status 写 unknown 并在 rationale 说明。
- {BASIS_RULES}
- 假设（assumptions）写模型依赖但无法从资料证实的前提，id 以 assumption_ 开头，affects 列出受影响的元素 ID。""",
    "structure": f"""生成编队几何、协同配置和自定义消息。
- 编队：长机（被跟随角色）位于编队原点，**不列入 slots**；slots 只列跟随角色的每个实例。
  role_instance 写 "<角色>[i]"，i 从 0 开始，小于该角色的 count（如 count 3 → [0]、[1]、[2]）。
  forward/right 用 {{"parameter_ref": 参数名, "scale": 倍数}}（后方/左侧用负数）或 {{"literal_distance": {{"value": 0, "unit": "m"}}}}。
  tolerance 只写 {{"parameter_ref": 容差参数}}，不要写 scale。
- 协同：每个协同只填写其 pattern 需要的字段，不要填写其他模式的字段：
  leader_follower → leader、followers、formation_ref（{{"blackboard_value": "<长机角色>.mission.<变量>"}} 或 {{"literal": "<编队ID>"}}）、可选 resync_interval；
  bounding_overwatch → groups（键必须恰好是 "bounding" 和 "overwatch"，值为 "<角色>" 或 "<角色>[i]" 列表）、bound_distance、active_when（条件表达式）、
    可选 standoff_distance、objective；
  report_and_wait → reporter、receiver（都必须是骨架中的角色）、ack_timeout（{{"value": 30, "unit": "sec"}}）；
  synchronized_transition → initiator、participants。
- 每个编队、协同都写 basis。{BASIS_RULES}
- 消息只写协同模式之外确实需要的自定义消息（from、to 为角色），通常为空列表。
- {NEW_PARAMETERS}""",
    "conditions": f"""生成条件（布尔表达式）。
- 条件 id 英文蛇形；expression 必须是下面 DSL 中的一个 JSON 对象，不能为空对象。阈值一律 parameter_ref 引用参数。
- 运算符全部小写（all、any、not、compare）。组件操作数写明组件 ID：{{"comm_value": {{"comm": "<通信组件ID>", "property": "available"}}}}，
  sensor_value、weapon_value 同理（键为 sensor、weapon）。
- 条件被多个角色共用；读取黑板变量时，该变量必须在每个使用此条件的角色中声明，或写成 "<角色>.mission.<变量>" 读取指定角色的黑板。
- 可以引用协同展开生成的条件。
- {NEW_PARAMETERS}""",
    "behaviors": f"""生成行为（行为树的叶节点）。
- actions 只能用动作目录中的命令，参数名与目录一致；参数值为操作数（如 {{"parameter_ref": "travel_speed"}}）或 ID 字符串（route、formation、message）。
  route 写一个路线 ID（如 "main_route"），L3 想定的 environment.routes 中会给出该路线坐标。
- 外部动作通常 issue_policy: "once_per_activation"；keep_station 等需要持续下发的由目录标注。
- precondition、success.expression、failure.expression 都必须是 DSL 表达式对象（不能为空对象）；成功即完成的瞬时行为写 {{"literal": true}}。
- 设置黑板变量用 set_blackboard（key 写 "mission.<变量>"，value 为操作数），并在 blackboard_variables 中声明该角色的变量。
  黑板变量类型只有 bool、int、double、string，**不能存放距离、速度等物理量**；需要阈值时直接用 parameter_ref。
- 表达式规则同条件段：运算符小写，组件操作数写明组件 ID（如 {{"comm_value": {{"comm": "comm_0", "property": "available"}}}}）。
- wait 等动作不能每 tick 下达，issue_policy 用 once_per_activation。
- 占用移动的行为在 claims 中写 ["movement_control"]。
- 协同模式已生成的行为直接在树中引用，不要重复定义。
- {NEW_PARAMETERS}""",
    "control": f"""生成行为树和状态转换。
- 行为树用扁平节点列表：每个节点给 node_id（全局唯一）、type、children（子节点 node_id 列表）；root 为根节点 id。
  type 取 sequence、sequence_with_memory、selector、selector_with_memory、parallel、behavior、condition、subtree、decorator。
- behavior 节点的 behavior 字段必须是"可引用的 ID"中的行为 ID（已定义的行为或协同生成的行为），**不能写动作名**（如 halt、follow_route）；
  condition 节点引用条件 ID；subtree 节点引用树 ID（可引用协同生成的 <协同ID>__tree）。
- 必须为骨架中每个状态引用的树给出定义。
- 转换：对每个角色的每个状态给出转换（role、state、transition_id 全局唯一、to、priority 越大越先、when 为 DSL 表达式对象）；
  每个状态最后加一条 when {{"literal": true}}、to 指向自身、priority 0 的兜底转换（不写 basis）。
- synchronize_with 只能填 leader_follower 或 synchronized_transition 类型的协同 ID，且只在发起方（长机）写；跟随方的同步转换由协同模式自动生成。
  带 synchronize_with 的转换，其目标状态必须是所有参与角色都有的状态（见"可引用的 ID"中的 sync_targets），否则不要写 synchronize_with。
- 协同生成的行为和条件只能在规定的角色中使用（见"可引用的 ID"中的 generated_usage），例如 report_and_wait 的
  __send_report、__await_ack 只给 reporter 用，__acknowledge 只给 receiver 用。
- 非兜底转换写 basis。{BASIS_RULES}""",
    "verification": """生成 L2 单步测试与 L3 闭环想定。
- L2 测试覆盖五类 category：nominal、precondition_failure、priority_transition、resource_unavailable、timeout_or_loss；
  given：role、instance（从 0 开始，小于 count）、state，以及需要的
    terrain（如 {"class": "urban", "distance_to_class.urban": {"value": 150, "unit": "m"}}）、
    tracks（如 {"nearest_hostile_range": {"value": 1, "unit": "km"}, "hostile_count": 1}）、
    components（如 {"mover.available": false}）、messages（[{"message": 消息ID, "fields": {...}}]）、
    elapsed（如 {"state": {"value": 3, "unit": "min"}}）、blackboard（键写 "mission.<变量>"）、
    coordination（键为协同 ID，如 {"<协同ID>": {"phase": 1}}）。
  expect 只写能确定的结果：next_state、fired_transition、tree_status（只能是 success、failure、running 之一）、
    selected_path（节点 ID 必须存在）、issued_actions（动作名）、sent_messages、blackboard；不得出现其他字段。
- L3 想定：每个协同和每个编队至少一个想定（在 traces_to 中列出其 ID）；
  environment：duration、routes（键为行为中使用的路线 ID，值为至少 2 个 [x, y] 坐标点，单位 m）、可选 terrain
    {"default_class": "open", "regions": [{"class": "urban", "x_min": 2500, "x_max": 4000, "y_min": -1500, "y_max": 1500}]}；
  initial：键为角色 ID（不带 [i]），如 {"<长机角色>": {"position": [0, 0], "heading_deg": 90, "state": "<状态>"}, "<跟随角色>": {"formation": "<编队ID>", "state": "<状态>"}}；
  events：可用 {"id": .., "type": "spawn_hostile", "at": {"value": 60, "unit": "sec"}, "position": [x, y]}、
    {"id": .., "type": "marker", "when": {"role": "<角色>", "x_gte": 2300}}、{"id": .., "type": "comm_degradation", "at": ..., "duration": ..., "loss": 0.5}；
  每个编队、每个协同至少出现在一个想定的 traces_to 中；至少一个想定包含 comm_degradation 或 message_loss 事件；
  每个 expect 中 role_state 等按角色观测的 observable 必须写 role。
  expect 至少一项：observable 取 role_state、formation_error_rms、min_interval、messages_sent、actions_issued、hostile_range_min，
    配合 role/formation/message/command，比较用 equals/lte/gte/lt/gt（数值写 {"value": .., "unit": ..} 或参数引用；
    messages_sent、actions_issued 的次数也用比较字段，如 {"gte": 1}，没有 count 字段），
    时间窗用 within {"after_event": 事件ID, "value": 秒数, "unit": "sec"} 或 during {"from": "start", "duration": {...}}。""",
}

SECTION_FORMATS: dict[str, str] = {
    "parameters": """{"parameters": [{"name": "column_interval", "type": "distance", "value": 50, "unit": "m", "minimum": 10, "maximum": 300,
  "description": "纵队车间距", "basis": {"status": "explicit", "evidence": ["ev_xxxxxxxxxx"]}, "implements": ["rq_03"]}],
 "assumptions": [{"id": "assumption_comms", "statement": "排内通信可用", "affects": ["coord_follow"], "requires_confirmation": true}]}""",
    "structure": """{"formations": [{"id": "column", "description": "纵队", "slots": [
   {"role_instance": "follower[0]", "forward": {"parameter_ref": "column_interval", "scale": -1}, "right": {"literal_distance": {"value": 0, "unit": "m"}}},
   {"role_instance": "follower[1]", "forward": {"parameter_ref": "column_interval", "scale": -2}, "right": {"literal_distance": {"value": 0, "unit": "m"}}}],
   "tolerance": {"parameter_ref": "station_tolerance"}, "basis": {"status": "user_specified"}}],
 "coordination": [
   {"id": "coord_follow", "pattern": "leader_follower", "leader": "leader", "followers": "follower",
    "formation_ref": {"blackboard_value": "leader.mission.current_formation"}, "basis": {"status": "inferred", "rationale": "...", "evidence": ["ev_..."]}},
   {"id": "coord_bound", "pattern": "bounding_overwatch", "groups": {"bounding": ["leader", "follower[1]"], "overwatch": ["follower[0]"]},
    "bound_distance": {"parameter_ref": "bound_distance"}, "standoff_distance": {"parameter_ref": "standoff_distance"},
    "active_when": {"condition_ref": "in_contact"}, "basis": {"status": "explicit", "evidence": ["ev_..."]}}],
 "messages": [], "new_parameters": []}""",
    "conditions": """{"conditions": [
  {"id": "in_contact", "description": "最近敌方进入接敌距离", "expression": {"compare": {"left": {"track_value": {"property": "nearest_hostile_range"}}, "operator": "lte", "right": {"parameter_ref": "contact_range"}}}},
  {"id": "mover_ok", "description": "机动部件可用", "expression": {"mover_value": "available"}},
  {"id": "leaving_urban", "description": "离开城区", "expression": {"all": [{"not": {"condition_ref": "entering_urban"}}, {"compare": {"left": {"terrain_value": "class"}, "operator": "ne", "right": {"literal": "urban"}}}]}}],
 "new_parameters": []}""",
    "behaviors": """{"behaviors": [
  {"id": "follow_route_open", "description": "沿主路线行进", "precondition": {"condition_ref": "mover_ok"}, "claims": ["movement_control"],
   "actions": [{"command": "follow_route", "arguments": {"route": "main_route", "speed": {"parameter_ref": "travel_speed_open"}}, "issue_policy": "once_per_activation"}],
   "success": {"expression": {"mover_value": "route_complete"}}, "failure": {"expression": {"not": {"condition_ref": "mover_ok"}}}},
  {"id": "set_formation_column", "description": "选择纵队", "actions": [{"command": "set_blackboard", "arguments": {"key": "mission.current_formation", "value": {"literal": "column"}}}],
   "success": {"expression": {"literal": true}}}],
 "blackboard_variables": [{"role": "leader", "blackboard": "mission", "name": "current_formation", "type": "string", "default": "wedge"}],
 "new_parameters": []}""",
    "control": """{"trees": [{"id": "leader_open_tree", "root": "open_root", "nodes": [
   {"node_id": "open_root", "type": "sequence_with_memory", "children": ["pick_wedge", "move_open"]},
   {"node_id": "pick_wedge", "type": "behavior", "behavior": "set_formation_wedge", "children": []},
   {"node_id": "move_open", "type": "behavior", "behavior": "follow_route_open", "children": []}]}],
 "transitions": [
   {"role": "leader", "state": "travel_open", "transition_id": "open_to_urban", "to": "travel_urban", "priority": 80,
    "when": {"condition_ref": "entering_urban"}, "synchronize_with": "coord_follow", "basis": {"status": "user_specified"}, "implements": ["rq_02"]},
   {"role": "leader", "state": "travel_open", "transition_id": "open_self", "to": "travel_open", "priority": 0, "when": {"literal": true}}]}""",
    "verification": """{"tests": [{"id": "leader_switches_near_urban", "category": "priority_transition",
   "given": {"role": "leader", "instance": 0, "state": "travel_open", "terrain": {"distance_to_class.urban": {"value": 150, "unit": "m"}}},
   "expect": {"next_state": "travel_urban", "fired_transition": "open_to_urban"}}],
 "scenarios": [{"id": "open_to_urban", "description": "接近城区时转纵队",
   "environment": {"terrain": {"default_class": "open", "regions": [{"class": "urban", "x_min": 2500, "x_max": 4000, "y_min": -1500, "y_max": 1500}]},
                   "routes": {"main_route": [[0, 0], [5000, 0]]}, "duration": {"value": 8, "unit": "min"}, "random_seed": 1},
   "initial": {"leader": {"position": [0, 0], "heading_deg": 90, "state": "travel_open"}, "follower": {"formation": "wedge", "state": "travel_open"}},
   "events": [{"id": "near_urban", "type": "marker", "when": {"role": "leader", "x_gte": 2300}}],
   "expect": [{"id": "e_switch", "observable": "role_state", "role": "follower", "equals": "travel_urban", "within": {"after_event": "near_urban", "value": 30, "unit": "sec"}}],
   "traces_to": ["open_to_urban", "coord_follow", "column"]}]}""",
}


def section_prompt(
    section: str,
    requirement: str,
    clauses: list[dict],
    skeleton: dict,
    done: dict[str, Any],
    catalog_text: str,
    claims: list[dict],
    generated_ids: dict[str, Any] | None,
    feedback: str = "",
    available_ids: dict[str, Any] | None = None,
    tactic: str = "",
) -> str:
    gen = f"\n协同展开生成的 ID（可直接引用）：\n{_json(generated_ids)}\n" if generated_ids else ""
    ids = (
        f"\n可引用的 ID（只能引用这里列出的，及本段自己定义的）：\n{_json(available_ids)}\n"
        if available_ids
        else ""
    )
    claim_block = (
        f"\n可引用的资料主张（evidence_id 只能从这里选）：\n{_json(claims)}\n" if claims else ""
    )
    return f"""你在为战术行为模型 IR 分段填写内容。本段：{section}。只输出一个 JSON 对象，不要输出其他文字。

{SECTION_GOALS[section]}

{EXPRESSION_GUIDE}
输出格式示例（字段与嵌套方式照此；示例中的 ID 和数值仅示意，不要照抄）：
{SECTION_FORMATS[section]}

目录：
{catalog_text}

需求：
{requirement}
{tactic}
条款：
{_json(clauses)}

骨架：
{_json(skeleton)}
{ids}{gen}
已完成的段：
{_json(done)}
{claim_block}{feedback}"""
