"""Real-error repair test (repair v2): the online repair loop (``fix_sheet``) run end to end on
historical sheets that the current checker fails (S0 index, ``status == fixable``), with a
given model. Unlike the injection test, nothing is known about the right fix: the checker
alone decides.

- Sheets whose only L1 failure is ``ROLE_COUNT`` are left out (checker drift since they
  were drafted, not a drafting error).
- The sheets are split by requirement (hash): ``test`` is the evaluation set; ``base`` is
  the pool whose sheets, once fixed, may serve as injection bases for training data
  (``--save-fixed``), so no requirement is both trained on and tested.
- Models: ``dashscope`` (the large model Falcon uses) or ``local:<name>`` (an
  OpenAI-compatible server, e.g. vLLM reached through an SSH tunnel, ``--base-url``).

Output: one JSON row per sheet (scores, steps, actions; no sheet text) and a summary. The
sheets and the fixed copies stay local.

Usage::

    python -m afsim_research_agent.training.realeval --index _local/training/real/index.json \
        --model local:repair-v2 --split test --out real_v2.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

from afsim_research_agent.falcon.llm import DashScopeModel, ModelRunner, parse_json_text
from afsim_research_agent.falcon.v3.fixer import fix_sheet
from afsim_research_agent.training import collect


class LocalModel:
    """An OpenAI-compatible chat endpoint serving the fine-tuned adapter. The prompt goes
    as one user message, thinking off, greedy: the format the samples were trained in."""

    def __init__(self, name: str, base_url: str, max_tokens: int = 1024) -> None:
        from openai import AsyncOpenAI

        self.name = name
        self.max_tokens = max_tokens
        self._client = AsyncOpenAI(base_url=base_url, api_key="local", timeout=300)

    async def generate(self, stage: str, prompt: str, schema: Any) -> dict[str, Any]:
        r = await self._client.chat.completions.create(
            model=self.name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=self.max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        text = r.choices[0].message.content or ""
        start = text.find("{")
        return parse_json_text(text[start:] if start >= 0 else text)


def split_of(row: dict[str, Any]) -> str:
    h = int(hashlib.sha1(str(row["requirement"]).encode()).hexdigest()[:8], 16)
    return "test" if h % 2 == 0 else "base"


def candidates(index: list[dict[str, Any]], split: str) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for i, row in enumerate(index):
        if row.get("status") != "fixable":
            continue
        if any("ROLE_COUNT" in f for f in row.get("failed", [])):
            continue
        if split != "all" and split_of(row) != split:
            continue
        out.append((i, row))
    return out


async def run_one(d: Any, model: Any, max_steps: int) -> dict[str, Any]:
    """``d``: the drafter from ``collect.load`` (built outside the event loop: it runs its own)."""
    start = d.check(verify=True)
    t0 = time.perf_counter()
    outcome = await fix_sheet(d, ModelRunner(model), max_steps=max_steps)
    end = d.check(verify=True)
    acts = Counter(r["action"] for r in outcome.log)
    return {
        "solved": bool(outcome.solved),
        "l1_start": bool(start.l1_ok),
        "l1_end": bool(end.l1_ok),
        "l1_problems_start": len(start.problems),
        "l1_problems_end": len(end.problems),
        "score_start": list(outcome.score_start),
        "score_end": list(outcome.score_end),
        "improved": tuple(outcome.score_end) < tuple(outcome.score_start),
        "steps": outcome.steps,
        "edits": len(outcome.edits),
        "actions": dict(acts),
        "parse_errors": acts.get("error", 0),
        "seconds": round(time.perf_counter() - t0, 1),
    }


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    return round(sum(bool(r.get(key)) for r in rows) / len(rows), 3) if rows else 0.0


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    l1 = [r for r in ok if not r["l1_start"]]
    return {
        "n": len(rows),
        "errors": len(rows) - len(ok),
        "solved": _rate(ok, "solved"),
        "improved": _rate(ok, "improved"),
        "l1_fixed": _rate(l1, "l1_end"),
        "l1_problems": [
            sum(r["l1_problems_start"] for r in ok),
            sum(r["l1_problems_end"] for r in ok),
        ],
        "mean_steps": round(sum(r["steps"] for r in ok) / len(ok), 1) if ok else 0,
        "mean_edits": round(sum(r["edits"] for r in ok) / len(ok), 1) if ok else 0,
        "parse_errors": sum(r["parse_errors"] for r in ok),
        "actions": dict(sum((Counter(r["actions"]) for r in ok), Counter())),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--model", required=True, help="dashscope | dashscope:<name> | local:<name>")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--split", choices=["test", "base", "all"], default="test")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--save-fixed", type=Path, default=None, help="copy solved sheets here")
    a = ap.parse_args()
    if a.model.startswith("local:"):
        model: Any = LocalModel(a.model[6:], a.base_url)
    else:
        model = DashScopeModel(a.model.split(":", 1)[1] if ":" in a.model else None)
    index = json.loads(a.index.read_text(encoding="utf-8"))
    todo = candidates(index, a.split)
    if a.limit:
        todo = todo[: a.limit]
    rows: list[dict[str, Any]] = []
    for i, row in todo:
        pkg = Path(row["pkg"])
        try:
            d = collect.load(pkg)
            res = asyncio.run(run_one(d, model, a.max_steps))
            if a.save_fixed is not None and res["solved"]:
                dst = a.save_fixed / f"r{i:03d}"
                dst.mkdir(parents=True, exist_ok=True)
                for name in ("falcon_request.yaml", "obligations.yaml"):
                    if (pkg / name).exists():
                        shutil.copy2(pkg / name, dst / name)
                (dst / "tactic_sheet.md").write_text(d.sheet, encoding="utf-8")
                res["fixed"] = dst.name
        except Exception as exc:  # noqa: BLE001 - one bad sheet does not stop the run
            res = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        rows.append({"index": i, "split": split_of(row), "requirement": row["requirement"]} | res)
        a.out.write_text(
            json.dumps({"model": a.model, "summary": summary(rows), "rows": rows},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )  # fmt: skip
    print(json.dumps(summary(rows), ensure_ascii=False))


if __name__ == "__main__":
    main()
