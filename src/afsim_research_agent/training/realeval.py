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


def _brief(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The loop's steps for a local log: action, arguments and the head of each result."""
    out = []
    for r in log:
        args = json.dumps(r.get("args") or {}, ensure_ascii=False)
        out.append(
            {
                "step": r.get("step"),
                "action": r.get("action"),
                "thought": str(r.get("thought") or "")[:200],
                "args": args[:600],
                "result": str(r.get("result") or "")[:700],
            }
        )
    return out


async def run_one(
    d: Any, model: Any, max_steps: int, afsim: Any = None, staging: Path | None = None
) -> dict[str, Any]:
    """``d``: the drafter from ``collect.load`` (built outside the event loop: it runs its
    own). ``afsim`` (an ``AfsimConfig``): once the 2D checks pass, the sheet runs in AFSIM
    and a failure goes back to the repair loop, as ``draft3 --afsim-*`` does."""
    start = d.check(verify=True)
    t0 = time.perf_counter()
    run = ModelRunner(model)
    outcome = await fix_sheet(d, run, max_steps=max_steps)
    logs = _brief(outcome.log)
    extra: dict[str, Any] = {}
    if afsim is not None and outcome.solved:
        from afsim_research_agent.falcon.v3.draft import _afsim_rounds

        fixes: list[Any] = []
        assert staging is not None
        staging.mkdir(parents=True, exist_ok=True)
        _check, history = await _afsim_rounds(d, run, d.check(verify=True), fixes, afsim, staging)
        rounds = [bool(r.passed) for r in history.rounds]
        extra = {
            "afsim_rounds": rounds,
            "afsim_first": bool(rounds and rounds[0]),
            # the last AFSIM round passed and the sheet it ran still passes the 2D checks
            "afsim_final": bool(rounds and rounds[-1] and d.check(verify=True).verified),
            "afsim_fix_steps": sum(f.steps for f in fixes),
        }
        for f in fixes:
            logs += [{"afsim_round": True}] + _brief(f.log)
    end = d.check(verify=True)
    acts = Counter(r["action"] for r in outcome.log)
    return (
        extra
        | {"_log": logs}
        | {
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
            # the model has fewer vehicles than its states need (often hidden behind the first
            # L1 problem): a drafting error, reported apart from the repair rate
            "role_count": any("ROLE_COUNT 模型只有" in str(r.get("result")) for r in outcome.log)
            or any(getattr(p, "code", "") == "ROLE_COUNT" for p in end.problems),
            "first_error": next(
                (r["result"][:160] for r in outcome.log if r["action"] == "error"), ""
            ),
            "seconds": round(time.perf_counter() - t0, 1),
        }
    )


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
        "afsim_first": _rate([r for r in ok if "afsim_rounds" in r], "afsim_first"),
        "afsim_final": _rate([r for r in ok if "afsim_rounds" in r], "afsim_final"),
        "solved_with_afsim": _rate(ok, "afsim_final"),
        "role_count": sum(bool(r.get("role_count")) for r in ok),
        "solved_without_role_count": _rate([r for r in ok if not r.get("role_count")], "solved"),
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
    ap.add_argument("--only", default="", help="comma-separated index numbers to run")
    ap.add_argument("--log-dir", type=Path, default=None, help="per-sheet step logs (local)")
    ap.add_argument("--afsim-binding", type=Path, default=None)
    ap.add_argument("--afsim-mission", default=None)
    ap.add_argument(
        "--afsim-run",
        action="append",
        default=[],
        help="<word in the package path>=<run config> or =kit:<scenario kit> (a run laid out "
        "from the model), e.g. strike=…/strike_allocation_v1.toml",
    )
    ap.add_argument("--afsim-rounds", type=int, default=3)
    ap.add_argument("--afsim-steps", type=int, default=8)
    ap.add_argument("--staging", type=Path, default=None, help="AFSIM work directory (local)")
    a = ap.parse_args()
    runs = dict(x.split("=", 1) for x in a.afsim_run)
    if a.model.startswith("local:"):
        model: Any = LocalModel(a.model[6:], a.base_url)
    else:
        from dotenv import load_dotenv

        load_dotenv()
        model = DashScopeModel(a.model.split(":", 1)[1] if ":" in a.model else None)
    index = json.loads(a.index.read_text(encoding="utf-8"))
    todo = candidates(index, a.split)
    if a.only:
        keep = {int(x) for x in a.only.split(",") if x.strip()}
        todo = [(i, r) for i, r in candidates(index, "all") if i in keep]
    if a.limit:
        todo = todo[: a.limit]
    rows: list[dict[str, Any]] = []
    for i, row in todo:
        pkg = Path(row["pkg"])
        try:
            d = collect.load(pkg)
            cfg = None
            word = next((w for w in runs if w in str(pkg)), None)
            if a.afsim_binding is not None and word is not None:
                from afsim_research_agent.falcon.v3.afsim_loop import AfsimConfig

                spec = runs[word]
                # "kit:<path>": the run is laid out from the model and its scenario binding
                # (the same values the 2D checks use), not a handwritten run
                kit = Path(spec[4:]) if spec.startswith("kit:") else None
                cfg = AfsimConfig(
                    binding=a.afsim_binding,
                    runs=[] if kit is not None else [Path(spec)],
                    mission=a.afsim_mission or "mission",
                    rounds=a.afsim_rounds,
                    steps=a.afsim_steps,
                    kit=kit,
                )
                if kit is not None:
                    from afsim_research_agent.ir.v2.afsim.scenario import load_kit

                    d.kit = load_kit(kit)
            stage = (a.staging or a.out.parent / "afsim_work") / f"r{i:03d}"
            res = asyncio.run(run_one(d, model, a.max_steps, cfg, stage))
            log = res.pop("_log", [])
            if a.log_dir is not None:
                a.log_dir.mkdir(parents=True, exist_ok=True)
                (a.log_dir / f"r{i:03d}.json").write_text(
                    json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            if a.save_fixed is not None and res["solved"]:
                dst = a.save_fixed / f"r{i:03d}"
                dst.mkdir(parents=True, exist_ok=True)
                for name in ("falcon_request.yaml", "obligations.yaml"):
                    if (pkg / name).exists():
                        shutil.copy2(pkg / name, dst / name)
                (dst / "tactic_sheet.md").write_text(d.sheet, encoding="utf-8")
                res["fixed"] = dst.name
        except Exception as exc:  # noqa: BLE001 - one bad sheet does not stop the run
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)[-6:]
            res = {
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                # where it was raised (file:line function), for a local look
                "where": [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames],
            }
        rows.append({"index": i, "split": split_of(row), "requirement": row["requirement"]} | res)
        a.out.write_text(
            json.dumps({"model": a.model, "summary": summary(rows), "rows": rows},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )  # fmt: skip
    print(json.dumps(summary(rows), ensure_ascii=False))


if __name__ == "__main__":
    main()
