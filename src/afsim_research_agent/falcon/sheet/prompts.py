"""Prompts for the tactic sheet (Chinese): structure, control and verification drafts,
the one-call variant, and the block repair prompt (design §3.4, §4)."""

from __future__ import annotations

import json
from typing import Any

from afsim_research_agent.capability.constraints import CONSTRAINT_NOTE, VocabularyConstraints
from afsim_research_agent.falcon.sheet.compiler import DEFAULT_SUCCESS, snake_to_camel
from afsim_research_agent.ir.catalog import Catalog

SHEET_PROMPT_VERSION = "falcon-sheet-2026.10.13"

# IR role-dependency capability IDs (ir/binding.py, capability/aliases.toml)
COMPONENT_CAPABILITIES = {
    "sensors": ("visual_search", "thermal_search", "radar_search"),
    "comms": ("voice_data", "data_link"),
    "weapons": ("direct_fire", "guided_missile"),
}
ROLE_FIELDS = (
    "count, platform（ground_vehicle/air_vehicle/surface_vessel/static_node）, description, "
    "superior（上级角色，可空）, mover（默认按平台；static_node 没有）, sensors/comms/weapons（{组件ID: 能力}；"
    f"传感器能力取 {'/'.join(COMPONENT_CAPABILITIES['sensors'])}，"
    f"通信取 {'/'.join(COMPONENT_CAPABILITIES['comms'])}，"
    f"武器取 {'/'.join(COMPONENT_CAPABILITIES['weapons'])}；组件 ID 自取英文蛇形名，没有的类别不写）"
)

STRUCTURE_GUIDE = """战术稿由若干带标签的代码块组成。本次只写两类块：

1. ```yaml model``` （一个）：
   - model：display_name、description、intent（意图词表）、concepts（概念词表，可空）。
   - roles：按任务中的平台与分工建角色，键为角色 ID（英文蛇形，不含 __）。字段：{role_fields}。
     同型、同分工的多个平台用一个角色加 count，实例写作 角色ID[0]、角色ID[1]…；分工不同的平台分成不同角色。
   - parameters：每个影响战术的数值一项：{{value: "50 m", range: ["10 m", "300 m"], description, basis, evidence, rationale}}。
     数值一律写成"数 单位"（m、km、km/h、s、min、deg）；计数写整数，开关写 true/false。
     需要机动的阶段，其速度也要列为参数（如 xxx_speed: {{value: "20 km/h"}}）；距离参数不能当速度用。
     basis 取 explicit（引用证据直接给出）、inferred（推断，写 rationale 并引用证据）、unknown（找不到依据，照常给合理参考值）、
     user_specified（仅当需求原文逐字给出该值）。evidence 只写资料主张的 ev_ 编号。
   - formations（可选）：只有任务要求多个平台保持相对位置时才写。以领队角色（leader_follower 的 leader）为原点，原点不列入；
     slots 写 {{"角色ID[0]": {{forward: -1 * 间距参数, right: "0 m"}}}}，forward 向前为正、right 向右为正，
     用"倍数 * 参数名"或"数 单位"；tolerance 写参数名。
   - coordination（可选）：角色之间需要配合时才写，键为协同 ID，pattern 从目录选（见下），字段按模式填写，模式的使用要点见目录。
     单个平台或各自独立行动的任务不写协同。
   - assumptions：{{ID: {{statement, affects: [元素]}}}}，写模型依赖但资料无法证实的前提。
   - implements：每条条款（rq_xx、tc_xx_…）列出实现它的元素：角色、"角色.状态"、编队、协同、参数，
     或转换 "角色: 起始状态 -> 目标状态"。所有条款都要出现，否则写入 out_of_scope。
   - out_of_scope：{{条款ID: 原因}}，只用于行为模型无法表达的内容（如政治目的、士气、指挥关系变更、模型外的单位或手段）。

2. ```mermaid <角色ID>``` （每个角色一张 stateDiagram-v2）：
   - `[*] --> 初始状态`；`起始 --> 目标 : 条件名`，同一状态的多条转换按书写顺序决定优先级（先写的优先）；
   - 标签只写条件名或 `not 条件名`（条件在下一步的 Python 块中定义，现在可以先使用名字）；
   - 状态按任务阶段划分（如 搜索 → 识别 → 报告 → 返航），每个状态是该角色在这一阶段持续做的事；
   - **任何阶段都可能发生的情况**（接敌、受损、通信中断等全局例外）写成 `* --> 目标状态 : 条件名`，
     编译器把它加到其他每个状态上，并优先于各状态自己的转换；不要只在某一个状态里写这类转换；
     例外的条件只写情况本身（如"发现敌情"），不要再与受损、距离等条件相与，否则多数时候不会触发；
   - 离开某状态的条件要考虑例外发生时的情况：例如"区域安全才前进"在有敌时永远不成立，
     此时必须有一条 `*` 例外转换把角色带走，否则会一直停在该状态；等待对方确认的状态要有超时出口
     （`确认条件 or time_in_state() >= 超时参数`），否则通信中断时会一直等下去；
   - `状态 : 中文说明` 给状态写说明；"留在本状态"的兜底转换不用写，也不要写回到本状态的转换；
   - 末尾加 `[sync]` 表示该转换由协同同步给其他角色（只有 leader_follower 的 leader 或 synchronized_transition 的
     initiator 可以写）。被同步的角色必须有同名状态，且**不要重复写**已同步的转换，只写它自己独有的转换。"""

# pattern → (fields to write, usage notes); notes are shown only for the patterns a model uses
PATTERN_FIELDS = {
    "leader_follower": "leader, followers, formations: {<leader 的状态>: <编队ID>, …}（leader 处于该状态时全组用这个编队；"
    "每个编队至少分配给一个状态；编队的写入与通报由编译器生成）, 可选 resync_interval",
    "bounding_overwatch": "bounding: [角色或实例], overwatch: [角色或实例], bound_distance: 参数名, active_when: 条件名, 可选 standoff: 参数名, objective",
    "report_and_wait": 'reporter（发报告的角色）, receiver（收报告并确认的角色）, ack_timeout: "30 s"；两者都必须是 roles 中的角色',
    "synchronized_transition": "initiator, participants",
}
PATTERN_STRUCTURE_NOTES = {
    "leader_follower": "同一 leader 与同一组 followers 只需**一个** leader_follower：变换编队写在它的 formations（状态 → 编队）里，不要每个编队各建一个协同。",
    "report_and_wait": "报告发给模型内的角色；需要向模型外的上级报告时，把该上级建成角色，或由 receiver 代为转达。",
}
PATTERN_TREE_RULES = {
    "leader_follower": "leader_follower：leader 自己机动（如 FollowRoute）；编队的切换与通报由编译器按 formations 生成，"
    "树里不用写编队变量和 announce_formation；followers 只用 协同ID.keep_station 保持站位，不要自己写 KeepStation。",
    "report_and_wait": "report_and_wait：reporter 用 协同ID.send_report 发报告、协同ID.await_ack 等确认；receiver 用 协同ID.acknowledge 确认。"
    "条件 协同ID.report_received 只对 receiver 成立，协同ID.ack_received 只对 reporter 成立。",
    "bounding_overwatch": "bounding_overwatch：参与交替行动的角色在该阶段的状态里调用 协同ID.tree()；"
    "需要单独控制时用 协同ID.bound / 协同ID.overwatch，条件 协同ID.my_turn 表示轮到本组行动。",
    "synchronized_transition": "synchronized_transition：initiator 的 [sync] 转换会让 participants 同时进入同名状态。",
}
PATTERN_TEST_RULES = {
    "leader_follower": '被 [sync] 同步的角色：given.messages 写 [{{协同ID.transition: {{to_state: 目标状态}}}}]，transition 写 "起始 -> 目标"。',
    "synchronized_transition": '被 [sync] 同步的角色：given.messages 写 [{{协同ID.transition: {{to_state: 目标状态}}}}]，transition 写 "起始 -> 目标"。',
    "report_and_wait": "协同ID.report_received / 协同ID.ack_received 在收到对应消息时成立：given.messages 写 [{{协同ID.report: {{}}}}] 或 [{{协同ID.ack: {{}}}}]。",
}


def used_patterns(symbols: dict[str, Any] | None) -> list[str]:
    """Coordination patterns the sheet uses (from the compiled symbol table)."""
    coordination = (symbols or {}).get("coordination") or {}
    return sorted({str(p) for p in coordination.values() if p})


def pattern_rules(table: dict[str, str], patterns: list[str] | None) -> str:
    """Notes for the patterns in use (all of them when not known yet)."""
    names = list(table) if patterns is None else [p for p in patterns if p in table]
    return "".join(f"\n   - {table[p]}" for p in names)


CONDITIONS_GUIDE = """```python conditions``` （一个）：每行 `条件名 = 表达式  # 中文说明`。
   - 结构稿状态图中用到的条件名都要定义（见"需要定义的条件"）。
   - 只允许比较（== != < <= > >=）、and / or / not、属性访问和下面列出的函数；参数直接写名字，数值写 "50 m" 或 q("50 m")。
   - 操作数：{namespaces}
   - 组件写法：sensor("组件ID").on、comm("组件ID").available（组件 ID 见"可引用的名字"）；黑板：bb.变量（本角色）、<角色>.bb.变量。
   - 协同生成的条件写成 协同ID.条件名（见"可引用的名字"）。
   - 函数：{functions}；另有简写 time_in_state() >= 参数、range_to_current_target() <= 参数。"""

TREE_RULES = """   - 行为树描述"在这个状态里持续做什么"。状态之间的转换条件已经写在状态图里，**不要**在树开头检查本状态的转换条件
     （那样条件成立前树什么也不做）；条件只用于状态内部的分支（如"目标在射程内才开火"）。
   - 需要机动的角色用机动动作（如 FollowRoute route="main_route"，路线由想定给出）；不需要机动的状态可以只做观察、报告或等待。
   - 动作的完成条件、前置条件由编译器按动作类型补全（持续运行的动作不需要 continuous）。
   - 速度、距离、时间端口只写 model.parameters 中的参数名或"数 单位"，不能读黑板（黑板不存物理量）。
   - 角色之间的消息只用协同生成的消息（见"可引用的名字"）；不要自造消息名。
   - **只能使用下面"动作节点"表中的动作**；表外的能力不要自造节点，写入 ```yaml needs```（见下）。{pattern_rules}"""

XML_TREES_GUIDE = """```xml <角色ID>``` （每个角色一个，BehaviorTree.CPP v4）：
   - `<root BTCPP_format="4">` 下每个状态一棵 `<BehaviorTree ID="状态名">`，恰好一个根节点；也可定义其他树供 `<SubTree ID="树名"/>` 复用。
   - 控制节点：Sequence（带记忆的顺序）、ReactiveSequence（每周期从头检查）、Fallback、ReactiveFallback、Parallel、
     Inverter、ForceSuccess、Repeat num_cycles、RetryUntilSuccessful num_attempts。
   - `<Once>` 包住的子节点每次进入本状态只执行一次（如报告一次），不需要自己设标记。
   - 叶子：动作节点（端口即参数，`{{{{变量}}}}` 读本角色黑板）；`<SetBlackboard key="变量" value="值"/>`（只在需要跨状态记住本角色自己的决定时用）；`<Condition ID="条件名"/>`；
     `<Coordination use="协同ID.行为名"/>`、`<SubTree ID="协同ID.tree"/>` 使用协同生成的行为和子树（只能在规定的角色中使用）。
   - 需要时可加 `_successIf="条件"`、`_failureIf="条件"`、`_while="条件"`、`timeout="30 s"`；可给节点加 name="..."（英文蛇形）。
{rules}"""

PY_TREES_GUIDE = """```python <角色ID>``` （每个角色一个）：用 Python 函数写行为树，编译器按固定规则转成行为树（不执行代码）。
   - 每个状态一个无参函数 `def 状态名():`，函数体的语句按顺序执行（Sequence）；其他函数是子树，在树中写 `函数名()` 调用。
   - 语句与对应的节点：
     动作 `FollowRoute(route="main_route", speed=travel_speed)`（端口=值；值写参数名、"数 单位"、字符串、数、bb.变量）；
     协同行为 `协同ID.行为名()`，协同子树 `协同ID.tree()`；条件检查 `assert 条件`；
     `with once():` 包住的语句每次进入本状态只执行一次（如报告一次、通报一次），不需要自己设标记；
     `if 条件:` … `else:` …（每周期重新判断）；`try:` A `except:` B（A 失败时做 B）；
     `with reactive():`（每周期从头检查）、`with fallback():`（依次尝试直到一个成功）、`with parallel(2):`（同时执行，2 个成功即成功）、
     `with repeat(3):`、`with retry(3):`、`with force_success():`、`with invert():`。
   - 黑板 `bb.变量 = "值"` 只在需要跨状态记住本角色自己的决定时使用（如选定的目标）；编队切换写在 formations，"只做一次"用 once。
   - 条件写条件名、`not 条件名`，或与 python conditions 相同的表达式。
   - 动作可加 `success_if="条件"`、`failure_if="条件"`、`while_="条件"`、`timeout="30 s"`、`name="节点名"`。
   - 不能用 while/for/return/import/函数参数；说明写成 # 注释。
   示例（另一个任务的片段，名字仅示意）：
   ```python scout
   def search():
       FollowRoute(route="main_route", speed=search_speed)

   def observe():
       with once():
           report_link.send_report()
       Halt()
   ```
{rules}"""

NEEDS_GUIDE = """```yaml needs``` （可选，没有就不写）：战术需要、但动作表与操作数中没有的能力。每项会成为能力缺口交给开发，行为树里先用替代写法：
   `需求ID: {{need: 一句话写清需要的能力, used_by: [角色 或 角色.状态], substitute: 能力到位前树里怎么做, clauses: [受影响的条款], gap: 已识别缺口编号（可选）}}`
   如果所选战术已识别的能力缺口（见下）就是这项需求，gap 写它的编号。不要为表中已有的动作登记需求。"""

BASIS_GUIDE = """```yaml basis``` （一个）：为每条非兜底转换写依据：
   `<角色>: {{起始状态 -> 目标状态: {{status: explicit, evidence: [ev_…]}}}}`，status 规则同参数；没有依据写 unknown。
   `*` 例外转换写一条 `* -> 目标状态` 即可。"""

TREE_FORMATS = ("python", "xml")


def control_guide(tree_format: str = "python", patterns: list[str] | None = None) -> str:
    """The control-stage guide; ``{namespaces}`` and ``{functions}`` are filled by the
    caller (``CONTROL_GUIDE.format`` compatible). ``patterns``: the coordination patterns
    the sheet uses; only their rules are included (all when None)."""
    trees = PY_TREES_GUIDE if tree_format == "python" else XML_TREES_GUIDE
    rules = TREE_RULES.replace("{pattern_rules}", pattern_rules(PATTERN_TREE_RULES, patterns))
    return f"""本次写四类块：

1. {CONDITIONS_GUIDE}

2. {trees.format(rules=rules)}

3. {BASIS_GUIDE}

4. {NEEDS_GUIDE}"""


CONTROL_GUIDE = control_guide("xml")

VERIFICATION_GUIDE = """本次只写 ```yaml verification``` 一个块：

tests（L2 单步测试；每条状态转换至少被一个测试触发；五类 category 都要有：nominal、precondition_failure、
priority_transition、resource_unavailable、timeout_or_loss）：
  - {{id, category, role, instance（从 0 开始）, state,
     given: {{terrain: {{class: <地形类别>}}, tracks: {{nearest_hostile_range: "1 km", hostile_count: 1}},
             components: {{mover.available: false}}, messages: [{{协同ID.消息: {{字段: 值}}}}], elapsed: {{state: "3 min"}}}},
     expect: {{next_state, transition: "起始 -> 目标"}}}}
  expect 只写战术意图：下一状态、触发的转换。编队（formations）由编译器自动检查，不用写。**不要写 actions、messages、status、blackboard**：
  这些机械结果由首次运行自动记录，交给人工核对。
  given 必须给出期望转换的条件所读取的输入，否则取默认值（条件通常不成立）：
  track.X → tracks.X；terrain.X → terrain.X（到某类地形的距离写 distance_to_class.<类别>）；team.X → team.X；
  formation.X → formation.X；self.X → platform.X；mover.X → components: {{mover.X: …}}；
  sensor("id").X / comm("id").X / weapon("id").X → components: {{id.X: …}}；状态计时 → elapsed: {{state: "3 min"}}；
  range_to_current_target() → tracks.current_target_range；formation_is("x") → formation.current_id。{pattern_tests}

scenarios（L3 闭环想定；覆盖主要战术过程；模型有编队或协同时，每个编队、每个协同至少出现在一个想定中；
模型有角色间通信时，至少一个想定包含 comm_degradation 事件）：
  - {{id, description, route: [[x, y], …]（米）, terrain: {{default: <地形类别>, regions: [{{class: <地形类别>, x: [x0, x1], y: [y0, y1]}}]}},
     duration: "8 min", seed: 1,
     events: [{{id, type: marker, when: {{role: <角色>, x_gte: 2300}}}} | {{id, type: spawn_hostile, at: "60 s", position: [x, y]}} |
              {{id, type: comm_degradation, at: "0 s", duration: "8 min", loss: 0.5}}],
     expect: [{{id, observable, role/formation/message/command, equals/lte/gte/lt/gt（参数名或"数 单位"）,
               within: {{after: 事件ID或期望ID, time: "30 s"}} 或 during: {{from: start, for: "150 s"}}}}],
     traces_to: [可选，编队/协同/"角色: 起始 -> 目标"]}}
  时间窗口要与距离和速度相符（时间 ≈ 路线距离 ÷ 速度参数）；与位置有关的阶段用 marker 事件
  （when: {{role: <角色>, x_gte: <位置>}}）作 within.after 的锚点，不要从 start 估算；敌方出现的位置和时间
  要让角色在想定时长内真的能遇到它。
  初始位置、编队初值由编译器补全；traces_to 也会按期望自动补充。
  观测量：{observables}"""


def verification_guide(observables: str, patterns: list[str] | None = None) -> str:
    return VERIFICATION_GUIDE.replace(
        "{pattern_tests}", pattern_rules(PATTERN_TEST_RULES, patterns).replace("\n   - ", "\n  ")
    ).format(observables=observables)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _mark(constraints: VocabularyConstraints | None, name: str) -> str | None:
    return "" if constraints is None else constraints.mark(name)


CATEGORY_NAMES = {
    "movement": "机动",
    "formation": "编队",
    "sensor": "传感器",
    "comms": "通信",
    "engagement": "火力",
    "control": "控制",
    "resource": "保障",
}


def _signature(name: str, ports: list[tuple[str, str, bool]], tree_format: str) -> str:
    camel = snake_to_camel(name)
    if tree_format == "python":
        args = ", ".join(f"{p}={t}{'!' if req else ''}" for p, t, req in ports)
        return f"{camel}({args})"
    attrs = " ".join(f'{p}="{t}{"!" if req else ""}"' for p, t, req in ports)
    return f"<{camel} {attrs}/>" if attrs else f"<{camel}/>"


def action_table(
    catalog: Catalog, constraints: VocabularyConstraints | None, tree_format: str = "xml"
) -> str:
    """The closed action vocabulary: every action the platform has, with ports (! =
    required), when it completes, what it needs, and the capability catalog entry it
    maps to (availability, limits, simplification)."""
    groups: dict[str, list[str]] = {}
    for action in catalog.actions.values():
        if action.name in ("set_blackboard", "delete_blackboard_value"):
            continue
        mark = _mark(constraints, action.name)
        if mark is None:
            continue
        ports = [(a.name, a.type, a.required) for a in action.arguments.values()]
        done = DEFAULT_SUCCESS.get(action.name, {"literal": True})
        finish = (
            "持续运行"
            if done is None
            else ("立即完成" if done == {"literal": True} else "到达/走完即完成")
        )
        needs = f"，需要{'/'.join(action.requires)}" if action.requires else ""
        line = f"- {_signature(action.name, ports, tree_format)}（{finish}{needs}）{mark}"
        info = constraints.details.get(action.name, {}) if constraints is not None else {}
        extra = []
        if info.get("catalog"):
            extra.append(f"目录：{info['catalog']}（{constraints.label(action.name)}）")  # type: ignore[union-attr]
        if info.get("simplified"):
            extra.append(f"简化：{info['simplified']}")
        elif info.get("limits"):
            extra.append(f"限制：{info['limits']}")
        if extra:
            line += " ｜ " + "；".join(extra)
        groups.setdefault(action.category, []).append(line)
    out = []
    for category, lines in groups.items():
        out.append(f"{CATEGORY_NAMES.get(category, category)}：")
        out += lines
    if constraints is not None:
        hidden = [n for n in constraints.names("unavailable") if n in catalog.actions]
        if hidden:
            out.append(
                f"（平台不具备、未列出：{'、'.join(snake_to_camel(n) for n in hidden)}；写了会被编译器拒绝）"
            )
    return "\n".join(out)


def known_gaps_block(
    gaps: list[dict[str, Any]] | None, constraints: VocabularyConstraints | None = None
) -> str:
    """Crow's capability gaps of the selected tactic, for ``needs.gap``. Gaps whose
    capability the vocabulary has (e.g. unconfirmed terrain queries) are shown with the
    element to use instead, so they are not registered as needs."""
    if not gaps:
        return ""
    outside, inside = [], []
    for g in gaps[:20]:
        name = g.get("display_name") or g.get("contract")
        element = constraints.element_for(str(g.get("contract") or "")) if constraints else None
        if element is not None and constraints is not None:
            inside.append(f"- {name}：词表已有 {element}（{constraints.label(element)}），直接使用")
        else:
            outside.append(f"- {g.get('id')}：{name}（{g.get('gap_type', '')}）")
    out = ""
    if outside:
        out += (
            "\n所选战术已识别、词表中没有的能力（needs 的 gap 可引用这些编号）：\n"
            + "\n".join(outside)
            + "\n"
        )
    if inside:
        out += (
            "\nCrow 预估中需确认、但词表已有的能力（不要登记 needs，直接使用）：\n"
            + "\n".join(inside)
            + "\n"
        )
    return out


def namespaces(catalog: Catalog, constraints: VocabularyConstraints | None) -> str:
    parts = []
    for op in catalog.operands.values():
        short = op.name[: -len("_value")] if op.name.endswith("_value") else op.name
        props = []
        for p in op.properties.values():
            mark = _mark(constraints, f"{op.name}.{p.name}")
            if mark is None:
                continue
            arg = f'("{p.argument}")' if p.argument else ""
            props.append(f"{p.name}{arg}{mark}")
        if props:
            parts.append(f"{short}: {', '.join(props)}")
    return "；".join(parts)


def functions(catalog: Catalog, constraints: VocabularyConstraints | None) -> str:
    out = []
    for f in catalog.functions.values():
        mark = _mark(constraints, f.name)
        if mark is None:
            continue
        out.append(f"{f.name}({', '.join(f.arguments)}){mark}")
    return "、".join(out)


def patterns(catalog: Catalog) -> str:
    lines = []
    for p in catalog.patterns.values():
        gen = "；".join(f"{k}: {', '.join(v)}" for k, v in p.generated.items() if v)
        note = PATTERN_STRUCTURE_NOTES.get(p.name)
        lines.append(
            f"- {p.name}：{p.description} 字段：{PATTERN_FIELDS.get(p.name, '')}。生成（协同ID.名字）：{gen}"
            + (f"\n  要点：{note}" if note else "")
        )
    return "\n".join(lines)


def _context(requirement: str, clauses: list[dict], tactic: str, claims: list[dict]) -> str:
    claim_block = (
        f"\n资料主张（evidence 只能用这里的 ev_ 编号）：\n{_json(claims)}\n" if claims else ""
    )
    return f"""需求：
{requirement}
{tactic}
条款：
{_json(clauses)}
{claim_block}"""


def structure_prompt(
    *,
    requirement: str,
    clauses: list[dict],
    tactic: str,
    claims: list[dict],
    platforms: list[dict],
    catalog: Catalog,
    intents: list[str],
    concepts: list[str],
    terrain: list[str],
    example: str,
    feedback: str = "",
) -> str:
    return f"""你在为战术行为模型写"战术稿"的结构部分。用 Markdown 代码块输出，不要输出其他文字。

{STRUCTURE_GUIDE.format(role_fields=ROLE_FIELDS)}

协同模式目录：
{patterns(catalog)}

意图词表：{_json(intents)}
概念词表：{_json(concepts)}
地形类别：{_json(terrain)}
平台提示：{_json(platforms) if platforms else "（未指定）"}

格式示例（另一个模型的片段，名字和数值仅示意）：
{example}

{_context(requirement, clauses, tactic, claims)}{feedback}"""


def symbol_list(symbols: dict[str, Any], usage: dict[str, str] | None = None) -> str:
    lines = [
        f"角色与状态：{_json(symbols.get('roles', {}))}",
        f"参数：{_json(symbols.get('parameters', []))}",
        f"编队：{_json(symbols.get('formations', []))}",
        f"协同：{_json(symbols.get('coordination', {}))}",
    ]
    if symbols.get("needed_conditions"):
        lines.append(f"需要定义的条件（状态图中已使用）：{_json(symbols['needed_conditions'])}")
    if symbols.get("conditions"):
        lines.append(f"已定义的条件：{_json(symbols['conditions'])}")
    if symbols.get("generated"):
        lines.append("协同生成（写成 协同ID.名字）：" + _json(symbols["generated"]))
    if symbols.get("transitions"):
        lines.append(f"转换：{_json(symbols['transitions'])}")
    if usage:
        lines.append(
            "协同生成的行为/条件只能由规定角色使用："
            + _json({k.replace("__", "."): v for k, v in sorted(usage.items())})
        )
    return "\n".join(lines)


def control_prompt(
    *,
    sheet: str,
    symbols: dict[str, Any],
    usage: dict[str, str] | None,
    catalog: Catalog,
    constraints: VocabularyConstraints | None,
    claims: list[dict],
    tactic: str,
    feedback: str = "",
    tree_format: str = "python",
    known_gaps: list[dict[str, Any]] | None = None,
) -> str:
    note = f"\n{CONSTRAINT_NOTE}\n" if constraints is not None else ""
    claim_block = (
        f"\n资料主张（basis 的 evidence 只能用这里的 ev_ 编号）：\n{_json(claims)}\n"
        if claims
        else ""
    )
    return f"""你在为战术行为模型写"战术稿"的控制部分。用 Markdown 代码块输出，不要输出其他文字。

{control_guide(tree_format, used_patterns(symbols)).format(namespaces=namespaces(catalog, constraints), functions=functions(catalog, constraints))}
{note}
动作节点（完整动作表；表外节点会被编译器拒绝）：
{action_table(catalog, constraints, tree_format)}
{known_gaps_block(known_gaps, constraints)}
已有的战术稿（结构部分，不要重写）：
{sheet}

可引用的名字：
{symbol_list(symbols, usage)}
{tactic}{claim_block}{feedback}"""


def transition_checklist(symbols: dict[str, Any] | None) -> str:
    """The transitions L2 tests must fire, one line each (the model does not count well)."""
    items = (symbols or {}).get("transition_checklist") or []
    if not items:
        return ""
    lines = []
    for t in items:
        inputs = f"；条件读取 {', '.join(t['inputs'])}" if t.get("inputs") else ""
        lines.append(f"- {t['role']}: {t['transition']}（条件 {t['when']}{inputs}）")
    return (
        f"\n必须测试的转换（共 {len(items)} 条；每条至少一个测试，role、state 写转换的起点，"
        "expect.transition 写这条转换，given 设定让条件成立的输入）：\n" + "\n".join(lines) + "\n"
    )


def verification_prompt(
    *,
    sheet: str,
    symbols: dict[str, Any],
    usage: dict[str, str] | None,
    observables: str,
    feedback: str = "",
) -> str:
    return f"""你在为战术行为模型写"战术稿"的验证部分。用 Markdown 代码块输出，不要输出其他文字。

{verification_guide(observables, used_patterns(symbols))}
{transition_checklist(symbols)}
已有的战术稿（不要重写）：
{sheet}

可引用的名字：
{symbol_list(symbols, usage)}
{feedback}"""


def repair_prompt(
    *,
    label: str,
    numbered: str,
    problems: list[str],
    sheet: str,
    symbols: dict[str, Any],
    usage: dict[str, str] | None,
    guide: str,
    reverted: list[str] | None = None,
    stuck: bool = False,
) -> str:
    listed = "\n".join(problems[:40])
    more = f"\n（另有 {len(problems) - 40} 条未列出）" if len(problems) > 40 else ""
    if stuck:
        more += (
            "\n\n上一轮已经要求修改这一块，但上面的问题原样还在：之前的改法不对。"
            "请先读每条问题后面的说明和可选项，换一种写法；不要再交回同样的内容。"
        )
    if reverted:
        more += (
            "\n\n上一次重写这一块后反而出现了下面这些问题，那次修改已撤回；"
            "这次修正上面的问题时，也不要引入它们：\n" + "\n".join(reverted[:20])
        )
    return f"""战术稿中 ```{label}``` 这一块有以下问题（行号是整份战术稿中的行号）：
{listed}{more}

请只重写这一块，修正上述问题，其他内容保持不变；名字要与战术稿其他块一致。输出一个 ```{label}``` 代码块，不要输出其他文字。

本块原文（带行号）：
{numbered}

写法说明：
{guide}

可引用的名字：
{symbol_list(symbols, usage)}

整份战术稿（供参考，不要重写其他块）：
{sheet}"""


def one_call_prompt(*, structure: str, control_guide: str, verification_guide: str) -> str:
    return f"""{structure}

另外，在同一次回答中继续写控制部分与验证部分：
{control_guide}

{verification_guide}"""
