"""Assemble the AFSIM tactical-modeling deep agent.

``build_agent`` wires the pieces described in agent-loop-deepagents-v1 §3:

- tools: every stage in ``tools.TOOL_SPECS`` plus ``ask_human``;
- filesystem: ``FilesystemBackend`` rooted at the data directory (virtual paths), so the
  built-in ``read_file`` / ``ls`` / ``grep`` see the same paths as the stage tools;
- permissions: ``_local`` denied, writes only under ``/runs``;
- human in the loop: tools flagged ``needs_approval`` pause before running;
  ``ask_human`` pauses inside the tool (LangGraph ``interrupt``);
- checkpointer: required for pausing/resuming outside ``langgraph dev``;
- planning: LangChain ``TodoListMiddleware`` (``write_todos``); deepagents 0.7.23 does not
  add it by default;
- subagents / skills: optional, empty by default (P2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from afsim_research_agent.orchestrator.extensions import ExtensionRegistry, get_registry
from afsim_research_agent.orchestrator.tools import approval_tools, build_tools
from afsim_research_agent.orchestrator.workspace import Workspace

AGENT_NAME = "afsim_tactical_agent"

SYSTEM_PROMPT = """始终用中文回答用户，包括简短的过程说明。

你是 AFSIM 战术行为建模 agent 的总控。用户用自然语言提出战术需求，你按流程调用工具，产出经过检查的行为模型。

## 流程
1. overview：了解流程和已接入的扩展能力。
2. crow_research：检索资料并通过 Falcon 输入门槛。门槛未通过时向用户说明缺口，不要硬往下走。
3. crow_propose：提出候选战术并经红队评审。把候选、得分、可行性和红队意见简要列给用户。
4. select_tactic：只记录用户明确说出的选择，不替用户选。
5. falcon_draft3：把选定战术生成战术稿 v3 与 IR 2.0 模型包，自带 L1–L3 检查和修复循环（本地已配置 AFSIM 时可加 afsim=true，在循环中运行 AFSIM）。起草前逐条核对战术条款能否用能力集实现：核心要素（步骤、队形）缺能力时默认不起草（状态 needs_capability，告诉用户缺什么、可另选战术或等能力开发）；用户同意不实现这些要素时用 missing_capabilities=skip，要求等能力开发时用 wait。结果为 draft_unverified 时说明哪些条款没有被运行判定。
   角色名：用 role_map 把 Crow 的角色（如 排长车、僚车）对应到角色 ID（如 leader、member）；要接 AFSIM 时必须与 AFSIM 运行配置中的角色一致，不清楚就用 ask_human 问。
   旧流程（falcon_draft → falcon_check / falcon_fix）只在用户要求时使用。
6. simulate：二维想定。静态检查通过不代表战术正确，必须看仿真结果。
7. confirmation_status / apply_confirmations：人工确认清单。
8. ir_build；平台后端（platform_export）已接入时再导出到 AFSIM。

## 规则
- 最终是否通过只看检查工具（falcon_draft3、falcon_check、simulate、ir_build）的结果，不要自己宣布通过。
- 遇到战术意图问题、需求矛盾，或 falcon_fix 返回 needs_human / 反复不收敛时，用 ask_human 向用户提问并给出选项，不要删改测试或想定来迁就模型。
- 用户决定某些测试先搁置、先看仿真时，用 defer_tests 把这些测试标为暂缓（不要删除或改写它们），然后重新 falcon_check、simulate；汇报时列出仍暂缓的测试。
- 扩展工具返回 not_configured 时，告诉用户该能力尚未接入，用现有流程继续。
- 行为树由程序生成：已接入 compile_plan 时只写高层描述，不要直接手写行为树 XML。
- 工具结果中的路径是工作区路径，可以用 read_file 打开查看；不要复述大段文件内容。
- 不要把本地受控资料、想定数据、平台参数写进检索请求；_local 目录不可访问。
- 不覆盖已有文件：新产物放在新的目录下。
- 用中文简要汇报每一步的结果和下一步。
- 某一步失败时先读失败原因再决定是否重试；同样的失败重复两次就停下来，用 ask_human 说明原因和可选做法。
"""


def filesystem_permissions() -> list[Any]:
    """Rules for the built-in file tools (first match wins), mirroring ``Workspace``."""
    from deepagents import FilesystemPermission

    return [
        FilesystemPermission(
            operations=["read", "write"], paths=["/_local/**", "/**/_local/**"], mode="deny"
        ),
        FilesystemPermission(operations=["write"], paths=["/runs/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]


def orchestrator_model_name(settings=None) -> str:
    """``ORCHESTRATOR_MODEL`` if set, else ``DASHSCOPE_CHAT_MODEL``."""
    import os

    from afsim_research_agent.config import Settings

    settings = settings or Settings.from_environment()
    return os.getenv("ORCHESTRATOR_MODEL") or settings.chat_model


def default_model(settings=None, model_name: str | None = None):
    """DashScope (qwen) through the OpenAI-compatible chat completions API."""
    from langchain_openai import ChatOpenAI

    from afsim_research_agent.config import Settings

    settings = settings or Settings.from_environment()
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required")
    return ChatOpenAI(
        model=model_name or orchestrator_model_name(settings),
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        temperature=0,
        extra_body={"enable_thinking": False},
    )


def build_agent(
    model: Any = None,
    *,
    workspace: Workspace | None = None,
    registry: ExtensionRegistry | None = None,
    checkpointer: Any = None,
    falcon_model_factory: Callable[[], Any] | None = None,
    stage_kwargs: dict[str, dict[str, Any]] | None = None,
    subagents: Sequence[Any] = (),
    skills: list[str] | None = None,
    interrupt: bool = True,
    todos: bool = True,
    system_prompt: str = SYSTEM_PROMPT,
    debug: bool = False,
):
    """The compiled deep agent graph.

    ``checkpointer``: pass one (e.g. ``InMemorySaver()``) when running from code or the
    CLI; leave ``None`` under ``langgraph dev`` / LangGraph server, which supplies its own.
    ``interrupt=False`` removes the approval pauses (tests / unattended dry runs only).
    """
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langchain.agents.middleware import TodoListMiddleware

    workspace = workspace or Workspace.from_settings()
    workspace.root.mkdir(parents=True, exist_ok=True)
    (workspace.root / "runs").mkdir(exist_ok=True)
    registry = registry if registry is not None else get_registry()
    tools = build_tools(
        workspace, registry, falcon_model_factory=falcon_model_factory, stage_kwargs=stage_kwargs
    )
    interrupt_on = (
        {name: {"allowed_decisions": ["approve", "edit", "reject"]} for name in approval_tools()}
        if interrupt
        else None
    )
    return create_deep_agent(
        model=model if model is not None else default_model(),
        tools=tools,
        system_prompt=system_prompt,
        subagents=list(subagents),
        skills=skills,
        backend=FilesystemBackend(root_dir=Path(workspace.root), virtual_mode=True),
        permissions=filesystem_permissions(),
        interrupt_on=interrupt_on,
        checkpointer=checkpointer,
        name=AGENT_NAME,
        debug=debug,
        middleware=[TodoListMiddleware()] if todos else [],
    )
