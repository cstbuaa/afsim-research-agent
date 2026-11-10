"""Score a local model's predictions on the pilot bundle with the real checker (run where
the capability set is, i.e. the Windows computer). Predictions come from the server
(``scripts/train/predict.py``): one JSON line per sample, ``{"id": …, "output": "…"}``.

- structure / control / verification: the predicted block(s) replace the gold ones; the
  rest of the sheet stays gold (the gold tests and scenarios are the fixed reference for
  structure and control, training plan §4.2). Reported: blocks parsed, L1, all checks.
- repair: the injected sheet is rebuilt from the sample id (``<spec>-<k>-<rule>-s<step>``,
  injection ``k`` of the gold sheet); a step-1 sample first gets the gold diagnosis. The
  predicted action runs through the repair loop's own tools. Reported: JSON parsed, the
  expected action, accepted by the loop, sheet solved.

Usage::

    python -m afsim_research_agent.training.score --bundle <dir> --pred preds.jsonl \
        --task repair --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.falcon.sheet.blocks import Block, split_sheet
from afsim_research_agent.falcon.sheet.generate import merge
from afsim_research_agent.falcon.v3.draft import CONTROL_KINDS, STRUCTURE_KINDS
from afsim_research_agent.falcon.v3.fixer import SheetFixer, _rebuild
from afsim_research_agent.training.inject import injections
from afsim_research_agent.training.render import drafter

KINDS = {
    "structure": STRUCTURE_KINDS,
    "control": CONTROL_KINDS,
    "verification": ("verification",),
}


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _ready(pair: dict[str, Any], blocks: list[Block]) -> Any:
    d = drafter(yaml.safe_load(pair["falcon_request"]), pair["requirement"], [])
    d.blocks = blocks
    d.slices = d._slices(d.compile(pending=True))
    asyncio.run(d.obligations_stage())
    return d


def _copy(blocks: list[Block]) -> list[Block]:
    return [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]


def score_sheet(task: str, pair: dict[str, Any], output: str) -> dict[str, Any]:
    gold = split_sheet(pair["tactic_sheet"])[0]
    kinds = KINDS[task]
    keep = [b for b in gold if b.kind not in kinds]
    got: list[Block] = []
    single = Block("verification", None, "", 0) if task == "verification" else None
    merge(got, output, kinds, target=single)
    want = {b.key for b in gold if b.kind in kinds}
    have = {b.key for b in got if b.kind in kinds}
    row = {"parsed": want <= have, "missing": sorted(want - have)}
    if not got:
        return row | {"l1": False, "verified": False}
    d = _ready(pair, _copy(keep) + got)
    check = d.check(verify=True)
    return row | {"l1": bool(check.l1_ok), "verified": bool(check.verified)}


def _action(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "action" in obj else None


def score_repair(
    row: dict[str, Any], pair: dict[str, Any], diagnosis: dict[str, Any] | None, output: str
) -> dict[str, Any]:
    m = re.match(r".+-(\d+)-(E\d+)-s(\d)$", row["id"])
    if m is None:
        return {"error": "id"}
    k, step = int(m.group(1)), int(m.group(3))
    inj = injections(pair["tactic_sheet"])[k]
    gold = split_sheet(pair["tactic_sheet"])[0]
    d = _ready(pair, _copy(gold))
    texts = {b.key: b.text for b in d.blocks}
    texts[inj.block] = texts[inj.block].replace(inj.before, inj.after)
    d.blocks = _rebuild(d.blocks, texts)
    fixer = SheetFixer(d)
    if step == 1 and diagnosis is not None:
        fixer.step = 1
        fixer.diagnose(diagnosis["args"])
    expected = json.loads(row["target"])["action"]
    act = _action(output)
    out: dict[str, Any] = {
        "rule": inj.rule,
        "stage": row["meta"]["stage"],
        "step": step,
        "parsed": act is not None,
        "action": (act or {}).get("action"),
        "expected_action": expected,
    }
    if act is None:
        return out | {"accepted": False, "solved": False}
    args = act.get("args") or {}
    fixer.step = step + 1
    if act["action"] == "diagnose":
        result = fixer.diagnose(args)
        where = args.get("where") or []
        out["accepted"] = result.startswith("诊断已记录")
        out["right_block"] = inj.block in (where if isinstance(where, list) else [where])
        out["solved"] = False
    elif act["action"] == "edit":
        before = fixer.score(fixer.last)
        result = fixer.edit(args.get("changes"), str(args.get("reason") or ""))
        out["accepted"] = result.startswith("已修改")
        out["improved"] = fixer.score(fixer.last) < before
        out["solved"] = bool(fixer.solved)
    else:
        out |= {"accepted": False, "solved": False}
    return out


def _summary(rows: list[dict[str, Any]], keys: list[str], group: str | None) -> dict[str, Any]:
    def rate(items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        return {"n": n} | {k: round(sum(bool(x.get(k)) for x in items) / n, 3) for k in keys}

    out: dict[str, Any] = {"all": rate(rows) if rows else {"n": 0}}
    if group:
        by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by[str(r.get(group))].append(r)
        out[group] = {g: rate(v) for g, v in sorted(by.items())}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--pred", type=Path, required=True)
    ap.add_argument("--task", choices=["structure", "control", "verification", "repair"])
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    pairs = {p["id"]: p for p in _load(a.bundle / "pairs" / "pairs_v3.jsonl")}
    preds = {p["id"]: p["output"] for p in _load(a.pred)}
    rows: list[dict[str, Any]] = []
    errors: Counter[str] = Counter()
    if a.task == "repair":
        samples = {r["id"]: r for r in _load(a.bundle / "repair" / "repair.jsonl")}
        for sid, output in preds.items():
            r = samples.get(sid)
            if r is None:
                errors["未知样本"] += 1
                continue
            s0 = samples.get(sid[:-1] + "0") if sid.endswith("s1") else None
            diagnosis = json.loads(s0["target"]) if s0 else None
            try:
                rows.append(
                    {"id": sid} | score_repair(r, pairs[r["meta"]["pair"]], diagnosis, output)
                )
            except Exception as exc:  # noqa: BLE001 - one bad sample does not stop the run
                errors[type(exc).__name__] += 1
        keys = ["parsed", "accepted", "solved"]
        report = {
            "by_action": _summary(
                [x | {"right_action": x.get("action") == x.get("expected_action")} for x in rows],
                ["right_action", *keys],
                "expected_action",
            ),
            "by_rule": _summary(rows, keys, "rule"),
            "edit_steps": _summary(
                [x for x in rows if x.get("expected_action") == "edit"], keys, "stage"
            ),
        }
    else:
        samples = {r["id"]: r for r in _load(a.bundle / "sft" / f"{a.task}.jsonl")}
        for sid, output in preds.items():
            r = samples.get(sid)
            if r is None:
                errors["未知样本"] += 1
                continue
            try:
                res = score_sheet(a.task, pairs[r["meta"]["pair"]], output)
                rows.append({"id": sid, "skeleton": r["skeleton"]} | res)
            except Exception as exc:  # noqa: BLE001
                errors[type(exc).__name__] += 1
        report = _summary(rows, ["parsed", "l1", "verified"], "skeleton")
    report["errors"] = dict(errors)
    a.out.write_text(
        json.dumps({"report": report, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
