"""Structured-output model interface for Falcon.

Every model call goes through :class:`ModelRunner`, which records stage, model
name, prompt hash and timing (never the raw prompt or response) and retries
once when the answer cannot be parsed into the requested schema.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import time
import typing
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.models import CallRecord, TextAnswer

T = TypeVar("T", bound=BaseModel)


class FalconModel(Protocol):
    """Returns either a schema instance or the parsed JSON object; the runner validates."""

    name: str

    async def generate(self, stage: str, prompt: str, schema: type[T]) -> T | dict[str, Any]: ...


class ModelOutputError(ValueError):
    """Answer that could not be turned into the schema; keeps the parsed JSON for salvage."""

    def __init__(self, cause: Exception, data: Any = None) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.data = data


def parse_json_text(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        text = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in text
        )
    body = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ModelOutputError(
            ValueError(f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）")
        ) from exc
    if not isinstance(data, dict):
        raise ModelOutputError(ValueError("顶层必须是 JSON 对象"), None)
    return data


class DashScopeModel:
    """Default model: the OpenAI-compatible DashScope endpoint Crow already uses."""

    def __init__(self, model: str | None = None) -> None:
        from afsim_research_agent.config import Settings

        self._settings = Settings.from_environment()
        self.name = model or os.getenv("FALCON_CHAT_MODEL") or self._settings.chat_model
        self._chat: Any = None
        self._bound: dict[tuple[type[BaseModel], str], Any] = {}

    def _client(self) -> Any:
        # created lazily so the Crow gate can refuse before credentials matter
        if self._chat is None:
            from langchain_openai import ChatOpenAI

            if not self._settings.dashscope_api_key:
                raise FalconError("FAL003", "DASHSCOPE_API_KEY is required for Falcon")
            self._chat = ChatOpenAI(
                model=self.name,
                api_key=self._settings.dashscope_api_key,
                base_url=self._settings.dashscope_base_url,
                temperature=0,
                timeout=float(os.getenv("FALCON_TIMEOUT_S", "180")),
                extra_body={"enable_thinking": False},
            )
        return self._chat

    async def generate(self, stage: str, prompt: str, schema: type[T]) -> T | dict[str, Any]:
        if schema is TextAnswer:
            message = await self._client().ainvoke(prompt)
            content = message.content
            if not isinstance(content, str):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            return {"text": content}
        method = structured_method(stage)
        if method == "json_mode":
            # parse and validate ourselves so errors are precise and fixable
            message = (
                await self._client().bind(response_format={"type": "json_object"}).ainvoke(prompt)
            )
            return parse_json_text(message.content)
        runnable = self._bound.get((schema, method))
        if runnable is None:
            runnable = self._client().with_structured_output(schema, method=method)
            self._bound[(schema, method)] = runnable
        answer = await runnable.ainvoke(prompt)
        if isinstance(answer, schema):
            return answer
        return schema.model_validate(answer)


def structured_method(stage: str) -> str:
    """Section outputs contain free-form expression objects. Strict ``json_schema``
    decoding cannot fill objects without declared fields (the model returns ``{}``),
    so sections use JSON mode with a format example in the prompt and Pydantic
    validation afterwards. ``FALCON_STRUCTURED_METHOD`` overrides for all stages."""
    forced = (os.getenv("FALCON_STRUCTURED_METHOD") or "").strip()
    if forced in ("json_schema", "json_mode"):
        return forced
    # fix-loop steps carry a free-form ``args`` object as well
    return "json_mode" if stage.startswith(("section:", "fix:", "properties:")) else "json_schema"


def describe_error(exc: Exception, limit: int = 8) -> str:
    """Short, model-readable description of why an answer was rejected."""
    if isinstance(exc, ModelOutputError):
        exc = exc.cause
    elif not isinstance(exc, ValidationError) and isinstance(exc.__cause__, ValidationError):
        exc = exc.__cause__  # langchain OutputParserException wraps the pydantic error
    if isinstance(exc, ValidationError):
        lines = [
            f"- {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()[:limit]
        ]
        more = len(exc.errors()) - limit
        return "\n".join(lines) + (f"\n- （另有 {more} 处）" if more > 0 else "")
    return f"- {type(exc).__name__}: {str(exc)[:300]}"


# Enum spellings models commonly use for the canonical values.
SYNONYMS = {
    "failed": "failure",
    "fail": "failure",
    "fails": "failure",
    "error": "failure",
    "succeeded": "success",
    "successful": "success",
    "succeed": "success",
    "ok": "success",
    "in_progress": "running",
    "pending": "running",
}
COMPARATORS = ("equals", "lte", "gte", "lt", "gt")


def _locate(data: Any, loc: tuple[Any, ...]) -> Any:
    node = data
    for key in loc:
        node = node[key]
    return node


def _fix_one(data: Any, err: dict[str, Any]) -> str | None:
    """Apply one mechanical fix for a validation error; return a note or None."""
    loc = tuple(err.get("loc", ()))
    if not loc:
        return None
    where = ".".join(str(p) for p in loc)
    try:
        parent = _locate(data, loc[:-1])
    except (KeyError, IndexError, TypeError):
        return None
    key = loc[-1]
    if err.get("type") == "literal_error" and isinstance(err.get("input"), str):
        expected = re.findall(r"'([^']*)'", str((err.get("ctx") or {}).get("expected", "")))
        value = err["input"].strip().lower()
        target = SYNONYMS.get(value) if SYNONYMS.get(value) in expected else None
        if target is None:
            close = difflib.get_close_matches(value, expected, n=1, cutoff=0.75)
            target = close[0] if close else None
        if target is not None and isinstance(parent, (dict, list)):
            parent[key] = target
            return f"{where}: '{err['input']}' → '{target}'"
    if err.get("type") == "extra_forbidden" and isinstance(parent, dict) and key in parent:
        value = parent.pop(key)
        if key == "count" and not any(c in parent for c in COMPARATORS):
            parent["gte"] = value  # "count: 1" on messages_sent / actions_issued means at least 1
            return f"{where}: count → gte {value}"
        return f"{where}: 删除未定义字段（值 {json.dumps(value, ensure_ascii=False)[:60]}）"
    return None


def coerce(schema: type[T], raw: Any, max_passes: int = 5) -> tuple[T, list[str]]:
    """Validate an answer, first fixing mechanical slips (enum spellings, unknown keys).

    Fixes are returned so they are recorded in the trace; anything else raises
    :class:`ModelOutputError` carrying the (partially fixed) data.
    """
    if isinstance(raw, schema):
        return raw, []
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(by_alias=True)
    data = copy.deepcopy(raw)
    fixes: list[str] = []
    for _ in range(max_passes):
        try:
            return schema.model_validate(data), fixes
        except ValidationError as exc:
            notes = [n for n in (_fix_one(data, e) for e in exc.errors()) if n]
            if not notes:
                raise ModelOutputError(exc, data) from exc
            fixes.extend(notes)
    try:
        return schema.model_validate(data), fixes
    except ValidationError as exc:
        raise ModelOutputError(exc, data) from exc


def salvage(schema: type[T], data: dict[str, Any]) -> tuple[T, list[str]]:
    """Keep the valid items of each list field and drop the rest (named in the notes)."""
    kept: dict[str, Any] = {}
    notes: list[str] = []
    for name, field in schema.model_fields.items():
        key = field.alias or name
        if key not in data:
            continue
        value = data[key]
        args = typing.get_args(field.annotation)
        if isinstance(value, list) and typing.get_origin(field.annotation) is list and args:
            adapter = TypeAdapter(args[0])
            items = []
            for index, item in enumerate(value):
                try:
                    items.append(adapter.validate_python(item))
                except ValidationError as exc:
                    ident = item.get("id") if isinstance(item, dict) else None
                    first = exc.errors()[0]
                    where = ".".join(str(p) for p in first["loc"])
                    notes.append(
                        f"已丢弃 {key}[{index}]{f'（{ident}）' if ident else ''}：{where} {first['msg']}"
                    )
            kept[key] = items
        else:
            kept[key] = value
    try:
        return schema.model_validate(kept), notes
    except ValidationError:
        return schema.model_validate({}), notes + ["整段无法使用，已置空"]


Handler = Callable[[str, type[BaseModel], int], Any]


class ScriptedModel:
    """Test double: answers each stage from a handler or a queue of answers.

    ``script[stage]`` is either a list (answers consumed in order, the last one
    repeats) or a callable ``(prompt, schema, call_index) -> answer``. Answers
    may be model instances or plain dicts.
    """

    name = "scripted"

    def __init__(self, script: dict[str, list[Any] | Handler]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    async def generate(self, stage: str, prompt: str, schema: type[T]) -> T | dict[str, Any]:
        key = stage if stage in self.script else stage.split(":", 1)[0]
        if key not in self.script:
            raise KeyError(f"no scripted answer for stage '{stage}'")
        index = sum(1 for s, _ in self.calls if s == stage)
        self.calls.append((stage, prompt))
        entry = self.script[key]
        if callable(entry):
            answer = entry(prompt, schema, index)
        else:
            answer = entry[min(index, len(entry) - 1)]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, BaseModel) and not isinstance(answer, schema):
            answer = answer.model_dump(by_alias=True)
        return answer


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class ModelRunner:
    """Wraps a model with call records, mechanical fixes and one parse retry.

    Section stages never abort the draft: when the retry still fails, the valid
    items are kept and the dropped ones are recorded, so the repair loop (which
    sees the gaps as L1/L2 problems) can regenerate them.
    """

    def __init__(self, model: FalconModel, retries: int = 1) -> None:
        self.model = model
        self.retries = retries
        self.records: list[CallRecord] = []

    def _record(self, stage: str, prompt: str, started: float, ok: bool, **extra: Any) -> None:
        self.records.append(
            CallRecord(
                stage=stage,
                model=self.model.name,
                prompt_sha256=prompt_hash(prompt),
                seconds=round(time.perf_counter() - started, 3),
                ok=ok,
                **extra,
            )
        )

    async def _generate(self, stage: str, prompt: str, schema: type[T], started: float) -> Any:
        """One model call; a timeout or dropped connection is retried twice (with a pause)
        before it fails the stage: one slow answer should not end a whole draft."""
        import asyncio

        for attempt in range(3):
            try:
                return await self.model.generate(stage, prompt, schema)
            except Exception as exc:  # noqa: BLE001 - only transport errors are retried
                name = type(exc).__name__
                transient = any(k in name for k in ("Timeout", "APIConnection", "RateLimit"))
                if not transient or attempt == 2:
                    raise
                self._record(stage, prompt, started, False, error=f"{name} (retried)")
                await asyncio.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    async def __call__(self, stage: str, prompt: str, schema: type[T]) -> T:
        last: Exception | None = None
        last_data: Any = None
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                raw = await self._generate(stage, prompt, schema, started)
                if isinstance(raw, str) and schema is TextAnswer:
                    raw = {"text": raw}
                answer, fixes = coerce(schema, raw)
            except (ValidationError, ValueError, TypeError) as exc:
                last = exc
                if isinstance(exc, ModelOutputError) and isinstance(exc.data, dict):
                    last_data = exc.data
                self._record(
                    stage,
                    prompt,
                    started,
                    False,
                    error=f"{type(exc).__name__}: {describe_error(exc)[:300]}",
                )
                if attempt < self.retries:
                    prompt = (
                        prompt
                        + "\n\n上一次输出不符合要求的 JSON 结构，问题如下；请修正后完整重新输出 JSON：\n"
                        + describe_error(exc)
                    )
                continue
            self._record(stage, prompt, started, True, fixes=fixes)
            return answer
        if stage.startswith("section:") and isinstance(last_data, dict):
            answer, notes = salvage(schema, last_data)
            self._record(stage, prompt, time.perf_counter(), True, fixes=notes, salvaged=True)
            return answer
        raise FalconError(
            "FAL004",
            f"stage '{stage}' returned unusable output",
            [describe_error(last)] if last else [],
        )
