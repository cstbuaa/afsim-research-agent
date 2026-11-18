"""The orchestrator stages exposed as LangChain tools for the deep agent.

``TOOL_SPECS`` is the single list of tools: name, stage function, argument schema,
description shown to the model, and flags (calls a model / needs human approval / extension).
``build_tools`` turns it into ``StructuredTool`` objects bound to one workspace.

Tool results are ``StageResult.render()`` JSON; artifacts are workspace paths that the
agent can open with the built-in ``read_file`` tool.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from afsim_research_agent.orchestrator import stages as st
from afsim_research_agent.orchestrator import stages3 as st3
from afsim_research_agent.orchestrator.extensions import ExtensionRegistry
from afsim_research_agent.orchestrator.workspace import Workspace

PKG = "模型包目录，工作区相对路径，如 /runs/crow-research/<运行>/models/<model_id>"


# ============================================================================ argument schemas
class NoArgs(BaseModel):
    pass


class CrowResearchArgs(BaseModel):
    requirement: str = Field(description="用户的自然语言战术需求原文")
    run_dir: str | None = Field(None, description="输出目录（runs/ 下、尚不存在）；缺省自动命名")
    max_sources: int | None = Field(None, ge=1, le=60, description="最多采用的来源数")
    gap_rounds: int | None = Field(None, ge=0, le=3, description="证据不足时追加检索轮数")


class CrowProposeArgs(BaseModel):
    run_dir: str = Field(description="crow_research 的运行目录")
    out_dir: str | None = Field(
        None, description="战术输出目录（尚不存在）；缺省在运行目录下自动命名"
    )
    redteam: bool = Field(True, description="是否进行红队评审与回应")


class SelectTacticArgs(BaseModel):
    tactics_dir: str = Field(description="crow_propose 输出的战术目录")
    choices: list[str] = Field(
        description="选中的候选，如 ['tc_01'] 或 ['tc_01:开阔地机动,队形转换']"
    )
    rejected: list[str] = Field(default_factory=list, description="明确否决的候选")
    note: str = Field("", description="用户的补充说明")
    by: str = Field(description="做出选择的人")


class FalconDraft3Args(BaseModel):
    tactics_dir: str = Field(description="Crow 战术目录（含用户确认的 tactic_selection.yaml）")
    model_id: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$", description="模型包名，小写字母数字下划线"
    )
    role_map: dict[str, str] = Field(
        default_factory=dict,
        description="Crow 角色 → 角色 ID，如 {排长车: leader, 僚车: member}；不给时按关键词推断",
    )
    requirement: str = Field("", description="补充的需求原文（默认用 Crow 的需求）")
    candidates: int = Field(3, ge=1, le=5, description="独立起草份数，取检查最好的一份")
    afsim: bool = Field(False, description="本地已配置 AFSIM 时，是否在起草循环中运行 AFSIM")
    missing_capabilities: Literal["block", "skip", "wait"] = Field(
        "block",
        description="能力集实现不了的战术要素：block 核心要素缺能力时不起草（默认），"
        "skip 不实现并在模型包中声明，wait 写成缺口等待能力开发",
    )
    drafting: Literal["skeleton", "free"] = Field(
        "skeleton",
        description="skeleton：先从已验证的骨架（编队搜索跟踪、集中分配交战）起草，没有适用骨架时自动从零起草"
        "（默认）；free：直接从零起草",
    )


class FalconDraftArgs(BaseModel):
    run_dir: str = Field(description="Crow 运行目录（含 research_result.json）")
    model_id: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$", description="模型包名，小写字母数字下划线"
    )
    tactics_dir: str | None = Field(None, description="含 tactic_selection.yaml 的战术目录")
    requirement: str = Field("", description="需求原文（未选战术时必填）")
    platforms: list[dict[str, Any]] = Field(
        default_factory=list,
        description="平台提示，如 [{role_hint: platoon_leader, capability_class: ground_vehicle, count: 1}]",
    )
    terrain_classes: list[str] = Field(
        default_factory=list, description="地形类别，如 ['open','urban']"
    )
    user_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="用户原话给出的数值，如 {column_spacing: {value: 50, unit: m}}",
    )
    with_l3: bool = Field(False, description="生成时是否要求二维想定也通过")


class PackageArgs(BaseModel):
    package: str = Field(description=PKG)


class CheckArgs(PackageArgs):
    with_l3: bool = Field(False, description="是否同时运行二维想定（L3）")


class FixArgs(PackageArgs):
    out_dir: str | None = Field(None, description="修正后的新目录；缺省 <模型包>_fix")
    max_steps: int = Field(30, ge=1, le=60)
    with_l3: bool = Field(False, description="L3 想定也必须通过")
    properties: bool = Field(True, description="生成并检查需求属性")


class PropertiesArgs(PackageArgs):
    force: bool = Field(False, description="覆盖已有的需求属性文件")


class DeferArgs(PackageArgs):
    test_ids: list[str] = Field(description="要暂缓的单步测试 ID")
    reason: str = Field(description="暂缓原因（写进战术稿和 decisions.yaml）")
    decision: str = Field("", description="用户的原话或所选选项")


class ConfirmArgs(PackageArgs):
    by: str = Field(description="确认人")


class BuildArgs(PackageArgs):
    backends: list[str] = Field(default_factory=list, description="后端子集；缺省全部")
    draft: bool = Field(False, description="草稿模式（未确认项不阻塞）")
    skip_l3: bool = Field(False)


class SimulateArgs(PackageArgs):
    scenario: str | None = Field(None, description="只跑某个想定 id")


class ScenarioArgs(PackageArgs):
    scenario: str | None = Field(
        None, description="scenario_set 写出的想定文件（工作区路径）；缺省用基准想定"
    )


class ScenarioSetArgs(PackageArgs):
    name: str = Field("", description="想定名（字母数字下划线），如 far7k；缺省按时间命名")
    end_time_s: float | None = Field(None, description="运行时长，秒")
    enemy_count: int | None = Field(None, description="敌方车辆数")
    enemy_range_m: float | None = Field(None, description="敌方起始距离（沿出发朝向），米")
    enemy_spacing_m: float | None = Field(None, description="敌方横向间距，米")
    enemy_approach_speed_mps: float | None = Field(
        None, description="敌方向我方开进的速度，米/秒；0 为静止"
    )
    enemy_stop_range_m: float | None = Field(None, description="敌方开进到距我方多远停下，米")
    enemy_approach_start_s: float | None = Field(
        None, description="敌方何时开始开进，秒；此前原地不动（如等我方停下监视后再推进）"
    )
    events: dict[str, float] = Field(
        default_factory=dict, description="模型事件的发出时间，秒；负数为结束前多少秒"
    )
    points: dict[str, list[float]] = Field(
        default_factory=dict, description="想定接口点的位置 [东, 北]，米，相对我方出发点"
    )
    base: str | None = Field(None, description="在这个已有想定文件的基础上修改")


class ScenarioVariantsArgs(ScenarioArgs):
    answers: dict[str, Any] = Field(
        default_factory=dict,
        description="scenario_plan 各题的回答 {题目 id: 选项 value}；没问到或用户没答的题用推荐值",
    )
    phase_times: dict[str, float] | None = Field(
        None, description="基准 AFSIM 运行中各状态首次进入的时刻 {状态: 秒}，用来安排分支的时间"
    )
    name: str = Field("branch", description="想定文件名前缀（字母数字下划线）")


class AfsimBranchesArgs(PackageArgs):
    scenarios: list[str] = Field(description="scenario_variants 写出的想定文件（工作区路径）")


class Simulate2dArgs(ScenarioArgs):
    only: str | None = Field(None, description="只跑某个想定 id（scenario_baseline 为基准想定）")


class AfsimRunArgs(ScenarioArgs):
    acceptance: str | None = Field(
        None, description="独立验收标准文件（obligations 列表，工作区路径），可选"
    )


class AskHumanArgs(BaseModel):
    question: str = Field(description="要用户决定的问题，写清背景和影响")
    options: list[str] = Field(
        default_factory=list, description="2–4 个可选答案（用户也可自由回答）"
    )
    directory: str | None = Field(None, description="把回答记入该目录的 decisions.yaml（runs/ 下）")


class ListActionsArgs(BaseModel):
    platform_class: str | None = Field(None, description="平台类别，如 ground_vehicle")


class FindTacticsArgs(BaseModel):
    query: str = Field(description="需求或战术描述")
    limit: int = Field(5, ge=1, le=20)


class CompilePlanArgs(BaseModel):
    plan: str = Field(description="按 plan_format 给出的格式写的高层描述全文")
    out_dir: str = Field(description="输出目录（runs/ 下、尚不存在）")


class PlatformExportArgs(PackageArgs):
    out_dir: str = Field(description="导出目录（runs/ 下、尚不存在）")
    backend: str = Field("afsim", description="平台后端名")
    run: bool = Field(True, description="导出后是否在平台上运行")


class PluginArgs(PackageArgs):
    gap_id: str = Field(description="capability_gaps.yaml 中的缺口 id")
    out_dir: str = Field(description="插件草稿目录（runs/ 下、尚不存在）")


# ============================================================================ specs
@dataclass(frozen=True)
class ToolSpec:
    name: str
    stage: Callable[..., Any]
    args: type[BaseModel]
    description: str
    group: str  # crow | falcon | scenario | ir | human | extension | meta
    uses_model: bool = False
    needs_approval: bool = False  # paused by HumanInTheLoopMiddleware before running


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "overview",
        st.overview,
        NoArgs,
        "查看流程顺序、扩展能力接入情况和最近的运行目录。开始时先调用一次。",
        "meta",
    ),
    ToolSpec(
        "crow_research",
        st.crow_research,
        CrowResearchArgs,
        "C1–C3：任务画像、研究规划、互联网检索、证据整理和 Falcon 输入门槛。耗时数分钟到半小时。",
        "crow",
        uses_model=True,
    ),
    ToolSpec(
        "crow_propose",
        st.crow_propose,
        CrowProposeArgs,
        "C4–C5：提出候选战术、标注平台可行性、红队评审与回应。之后把候选交给用户选择。",
        "crow",
        uses_model=True,
    ),
    ToolSpec(
        "select_tactic",
        st.select_tactic,
        SelectTacticArgs,
        "记录用户选定的战术（tactic_selection.yaml）。只能记录用户明确说出的选择；调用前会暂停等用户批准。",
        "human",
        needs_approval=True,
    ),
    ToolSpec(
        "falcon_draft3",
        st.falcon_draft3,
        FalconDraft3Args,
        "Falcon v3：把用户选定的 Crow 战术生成战术稿 v3 与 IR 2.0 模型包，并做 L1–L3 检查"
        "（可选在循环中运行 AFSIM）。优先使用这个工具。",
        "falcon",
        uses_model=True,
    ),
    ToolSpec(
        "scenario_show",
        st3.scenario_show,
        ScenarioArgs,
        "查看模型包所在的想定：运行时长、敌方数量/起始距离/开进速度、接口点与路线、事件时间。",
        "scenario",
    ),
    ToolSpec(
        "scenario_set",
        st3.scenario_set,
        ScenarioSetArgs,
        "按用户要求写一个新的想定文件（改敌方距离与速度、运行时长、事件时间、点的位置），"
        "不改模型包。调用前会暂停等用户批准。",
        "scenario",
        needs_approval=True,
    ),
    ToolSpec(
        "scenario_plan",
        st3.scenario_plan,
        ScenarioArgs,
        "找出模型里需要专门想定才能触发的战术分支（目标丢失、重新获取超时、敌方逼近撤离、"
        "上级命令），并给出要问用户的问题（带选项和推荐值）。之后用 ask_human 逐题提问。",
        "scenario",
    ),
    ToolSpec(
        "scenario_variants",
        st3.scenario_variants,
        ScenarioVariantsArgs,
        "按用户对 scenario_plan 问题的回答，为每个分支写一个想定变体文件（不改模型包）。"
        "调用前会暂停等用户批准。",
        "scenario",
        needs_approval=True,
    ),
    ToolSpec(
        "afsim_branches",
        st3.afsim_branches,
        AfsimBranchesArgs,
        "逐个运行分支想定（AFSIM），核对长车是否按期望经过该分支，并判定需求约束。"
        "调用前会暂停等用户批准。",
        "scenario",
        needs_approval=True,
    ),
    ToolSpec(
        "simulate2d",
        st3.simulate2d,
        Simulate2dArgs,
        "二维仿真：运行 v3 模型包的验证想定和一次基准想定，生成回放（工作台仿真面板可看）。",
        "scenario",
    ),
    ToolSpec(
        "afsim_run",
        st3.afsim_run,
        AfsimRunArgs,
        "在 AFSIM 中运行一次 v3 模型包（不修改模型），判定需求约束和可选的独立验收标准，"
        "生成回放。耗时约 1–5 分钟。调用前会暂停等用户批准。",
        "scenario",
        needs_approval=True,
    ),
    ToolSpec(
        "falcon_draft",
        st.falcon_draft,
        FalconDraftArgs,
        "旧版（v2）：按需求和选定战术生成战术稿与模型包；新流程用 falcon_draft3。",
        "falcon",
        uses_model=True,
    ),
    ToolSpec(
        "falcon_check",
        st.falcon_check,
        CheckArgs,
        "不改动模型包，运行校验、单步测试、探针、需求属性（可选 L3），列出问题。",
        "falcon",
    ),
    ToolSpec(
        "falcon_compile",
        st.falcon_compile,
        PackageArgs,
        "把 tactic_sheet.md 重新编译为 behavior_spec.yaml（不调用模型）。",
        "falcon",
    ),
    ToolSpec(
        "falcon_fix",
        st.falcon_fix,
        FixArgs,
        "自检纠错循环：在模型包副本上逐步修正战术稿，直到检查通过、需要人决定或用完步数。",
        "falcon",
        uses_model=True,
    ),
    ToolSpec(
        "falcon_properties",
        st.falcon_properties,
        PropertiesArgs,
        "不看战术稿，根据需求条款独立写出需求属性，并检查当前战术稿。",
        "falcon",
        uses_model=True,
    ),
    ToolSpec(
        "defer_tests",
        st.defer_tests,
        DeferArgs,
        "按用户决定把某些单步测试标为暂缓（不参加检查、保留在稿件中），让其余部分能编译和仿真。"
        "只在用户明确同意暂缓时使用；调用前会暂停等用户批准。",
        "human",
        needs_approval=True,
    ),
    ToolSpec(
        "confirmation_status",
        st.confirmation_status,
        PackageArgs,
        "查看人工确认清单进度和阻塞正式构建的项。",
        "human",
    ),
    ToolSpec(
        "apply_confirmations",
        st.apply_confirmations,
        ConfirmArgs,
        "把用户在 confirmations.yaml 中的决定写回模型。调用前会暂停等用户批准。",
        "human",
        needs_approval=True,
    ),
    ToolSpec(
        "ir_build",
        st.ir_build,
        BuildArgs,
        "规范化、测试并生成后端产物（Mermaid、BT.CPP XML、二维仿真），并做平台能力核对。",
        "ir",
    ),
    ToolSpec(
        "simulate",
        st.simulate,
        SimulateArgs,
        "旧版（v2 模型包）二维想定；v3 模型包用 simulate2d。",
        "ir",
    ),
    ToolSpec(
        "capability_gaps", st.capability_gaps, PackageArgs, "列出模型包的平台能力缺口。", "ir"
    ),
    ToolSpec(
        "list_actions",
        st.list_actions,
        ListActionsArgs,
        "【扩展：动作集】列出平台无关的原子动作。未接入时返回 not_configured。",
        "extension",
    ),
    ToolSpec(
        "find_tactics",
        st.find_tactics,
        FindTacticsArgs,
        "【扩展：战术集】按需求检索战术模板。未接入时返回 not_configured。",
        "extension",
    ),
    ToolSpec(
        "plan_format",
        st.plan_format,
        NoArgs,
        "【扩展：行为树生成器】查看高层描述的写法。写 compile_plan 的输入前先调用。",
        "extension",
    ),
    ToolSpec(
        "compile_plan",
        st.compile_plan,
        CompilePlanArgs,
        "【扩展：行为树生成器】把高层描述确定性地编译为行为树。不要自己直接写行为树 XML。",
        "extension",
    ),
    ToolSpec(
        "platform_export",
        st.platform_export,
        PlatformExportArgs,
        "【扩展：仿真平台】把模型包映射到 AFSIM 等平台并运行，返回报错。调用前会暂停等用户批准。",
        "extension",
        needs_approval=True,
    ),
    ToolSpec(
        "draft_capability_plugin",
        st.draft_capability_plugin,
        PluginArgs,
        "【扩展：能力插件】为某个能力缺口起草并验证平台插件。调用前会暂停等用户批准。",
        "extension",
        needs_approval=True,
    ),
)

ASK_HUMAN_DESCRIPTION = (
    "向用户提问并等待回答。遇到战术意图问题（如受损时继续跃进还是撤出）、需求矛盾、"
    "或纠错循环反复不收敛时使用，不要靠删改测试绕开。回答会记入 decisions.yaml。"
)


# the old (v2, IR 1.0) flow and its extensions: not given to the agent by default. They
# cannot read v3 packages (IR 2.0), and offering both made the agent try them on v3 packages
# and misread their errors as a broken package.
LEGACY_TOOLS = frozenset(
    {
        "falcon_draft",
        "falcon_check",
        "falcon_compile",
        "falcon_fix",
        "falcon_properties",
        "defer_tests",
        "confirmation_status",
        "apply_confirmations",
        "ir_build",
        "simulate",
        "capability_gaps",
        "plan_format",
        "compile_plan",
        "platform_export",
        "draft_capability_plugin",
    }
)
TOOLSETS = ("v3", "all")


def toolset_names(toolset: str = "v3") -> set[str]:
    """Tool names of a toolset: ``v3`` (the current flow, default) or ``all`` (with the old
    flow, for v2 packages and the offline check). ``ask_human`` is in both."""
    if toolset not in TOOLSETS:
        raise ValueError(f"unknown toolset {toolset!r}; one of {TOOLSETS}")
    names = {s.name for s in TOOL_SPECS} | {"ask_human"}
    return names if toolset == "all" else names - LEGACY_TOOLS


def approval_tools(include: set[str] | None = None) -> list[str]:
    return [
        s.name for s in TOOL_SPECS if s.needs_approval and (include is None or s.name in include)
    ]


# ============================================================================ building
def build_tools(
    workspace: Workspace,
    registry: ExtensionRegistry | None = None,
    *,
    falcon_model_factory: Callable[[], Any] | None = None,
    stage_kwargs: dict[str, dict[str, Any]] | None = None,
    include: set[str] | None = None,
) -> list[Any]:
    """StructuredTools bound to ``workspace``.

    ``falcon_model_factory`` injects the model for stages that call one (tests use a
    scripted model); ``stage_kwargs`` passes extra keyword arguments per tool name
    (e.g. a fake Crow pipeline).
    """
    from langchain_core.tools import StructuredTool

    tools = []
    for spec in TOOL_SPECS:
        if include is not None and spec.name not in include:
            continue
        tools.append(
            StructuredTool.from_function(
                func=_sync_runner(spec, workspace, registry, falcon_model_factory, stage_kwargs),
                coroutine=_async_runner(
                    spec, workspace, registry, falcon_model_factory, stage_kwargs
                ),
                name=spec.name,
                description=spec.description,
                args_schema=spec.args,
            )
        )
    if include is None or "ask_human" in include:
        tools.append(_ask_human_tool(workspace))
    return tools


def _call_kwargs(spec, workspace, registry, factory, stage_kwargs, args: dict[str, Any]):
    params = inspect.signature(spec.stage).parameters
    kwargs = {k: v for k, v in args.items() if k in params}
    if "registry" in params and registry is not None:
        kwargs["registry"] = registry
    if spec.uses_model and factory is not None and "model" in params:
        kwargs["model"] = factory()
    kwargs.update((stage_kwargs or {}).get(spec.name, {}))
    return kwargs


def _sync_runner(spec, workspace, registry, factory, stage_kwargs):
    def run(**args: Any) -> str:
        kwargs = _call_kwargs(spec, workspace, registry, factory, stage_kwargs, args)
        out = spec.stage(workspace, **kwargs)
        if inspect.isawaitable(out):
            out = _run_coroutine(out)
        return out.render()

    run.__name__ = spec.name
    return run


HEARTBEAT_S = 15.0


@contextlib.asynccontextmanager
async def _progress(tool: str):
    """While a tool runs: its progress lines (``afsim_research_agent.progress``) and a
    heartbeat go to the browser as LangGraph custom stream events (local only; not part
    of the conversation the model sees)."""
    from afsim_research_agent import progress

    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 - outside a graph run (tests, scripts)
        writer = None
    if writer is None:
        yield
        return
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = time.perf_counter()

    def emit(text: str, kind: str = "progress") -> None:
        payload = {
            "type": "workbench_progress",
            "tool": tool,
            "kind": kind,
            "t": round(time.perf_counter() - started, 1),
            "text": text,
        }
        if threading.get_ident() == loop_thread:
            writer(payload)
        else:  # a stage working in a thread (L3, AFSIM, retrieval)
            loop.call_soon_threadsafe(writer, payload)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            emit(f"仍在运行（已 {time.perf_counter() - started:.0f} s）", "heartbeat")

    emit("开始", "start")
    beat = asyncio.create_task(heartbeat())
    try:
        with progress.sink(emit):
            yield
    finally:
        beat.cancel()
        emit(f"结束（用时 {time.perf_counter() - started:.0f} s）", "end")


def _async_runner(spec, workspace, registry, factory, stage_kwargs):
    async def run(**args: Any) -> str:
        kwargs = _call_kwargs(spec, workspace, registry, factory, stage_kwargs, args)
        async with _progress(spec.name):
            if asyncio.iscoroutinefunction(spec.stage):
                out = await spec.stage(workspace, **kwargs)
            else:  # deterministic stages may be slow (L3): keep the event loop free
                out = await asyncio.to_thread(spec.stage, workspace, **kwargs)
        return out.render()

    run.__name__ = spec.name
    return run


def _run_coroutine(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ask_human_tool(workspace: Workspace):
    from langchain_core.tools import StructuredTool

    def ask_human(
        question: str, options: list[str] | None = None, directory: str | None = None
    ) -> str:
        from langgraph.types import interrupt

        answer = interrupt(
            {"type": "ask_human", "question": question, "options": list(options or [])}
        )
        text = answer if isinstance(answer, str) else str(answer.get("answer", answer))
        if directory:
            st.record_decision(workspace, directory, question, text, options=options)
        return f"用户回答：{text}"

    return StructuredTool.from_function(
        func=ask_human,
        name="ask_human",
        description=ASK_HUMAN_DESCRIPTION,
        args_schema=AskHumanArgs,
    )
