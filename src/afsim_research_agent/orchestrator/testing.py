"""A scripted chat model for offline checks of the agent wiring (no network, no keys).

Each scripted turn is either a final text or a list of tool calls. The model ignores
its input; it only lets ``create_deep_agent`` + the tools + interrupts run end to end.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def call(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> dict:
    return {"name": name, "args": args or {}, "id": call_id or f"call_{name}", "type": "tool_call"}


class ScriptedChatModel(BaseChatModel):
    """Returns the scripted turns in order; repeats the last one when it runs out."""

    turns: list[Any]
    index: int = 0
    seen_tools: list[str] = []  # noqa: RUF012 - names of the tools bound by the agent
    prompts: list[list[BaseMessage]] = []  # noqa: RUF012

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **_: Any) -> ScriptedChatModel:
        names = []
        for t in tools:
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if name is None and isinstance(t, dict):
                name = t.get("function", {}).get("name")
            names.append(name or str(t))
        self.seen_tools = sorted(set(self.seen_tools) | {n for n in names if n})
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **_: Any):
        self.prompts.append(list(messages))
        turn = self.turns[min(self.index, len(self.turns) - 1)]
        self.index += 1
        if isinstance(turn, str):
            msg = AIMessage(content=turn)
        else:
            calls = []
            for i, c in enumerate(turn):
                c = dict(c)
                c["id"] = f"{c.get('id', 'call')}_{self.index}_{i}"
                calls.append(c)
            msg = AIMessage(content="", tool_calls=calls)
        return ChatResult(generations=[ChatGeneration(message=msg)])
