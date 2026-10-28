"""Conformance pack for native backends (engines that execute a compiled model).

``export_pack`` writes, for one model package:

* ``normalized_ir.json`` — the compiler input;
* ``l2_cases.json`` — every L2 test: its inputs and the reference interpreter's
  full single-tick result (next state, fired transition, tree status, selected
  path, ticked nodes, issued actions with arguments, sent messages with fields,
  blackboard). A native backend must reproduce these from the same inputs;
* ``l3_reference.json`` — the built-in simulation's measured value for every
  scenario expectation, to compare with the judged trace of a native run.

``compare_l2`` checks a native backend's replay results against the pack.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.binding import bind
from afsim_research_agent.ir.l2 import run_test
from afsim_research_agent.ir.sim import run_scenarios

PACK_VERSION = "conformance-2026.10.1"
COMPARED_FIELDS = (
    "next_state",
    "fired_transition",
    "tree_status",
    "selected_path",
    "issued_actions",
    "sent_messages",
    "blackboard",
    "issued_action_details",
    "sent_message_details",
)


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def export_pack(ir: dict[str, Any], out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "normalized_ir.json").write_text(
        json.dumps(ir, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    cases = []
    for test in ir["tests"]:
        outcome = run_test(ir, test)
        cases.append(
            {
                "id": test["id"],
                "role": outcome.role,
                "given": test["given"],
                "expect": {k: v for k, v in test["expect"].items() if v is not None},
                "reference": outcome.result,
                "reference_passed": outcome.passed,
            }
        )
    (out_dir / "l2_cases.json").write_text(
        json.dumps(
            _clean({"pack_version": PACK_VERSION, "ir_hash": ir["ir_hash"], "cases": cases}),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    context, _ = bind(ir, "python_sim")
    scenarios = []
    if context is not None:
        for result in run_scenarios(ir, context):
            scenarios.append(result.to_dict())
    (out_dir / "l3_reference.json").write_text(
        json.dumps(
            _clean(
                {"pack_version": PACK_VERSION, "ir_hash": ir["ir_hash"], "scenarios": scenarios}
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"l2_cases": len(cases), "l3_scenarios": len(scenarios)}


def compare_l2(pack_dir: Path, results_path: Path) -> list[str]:
    """Compare a native backend's replay results with the pack.

    ``results_path`` is JSON: ``{"ir_hash": ..., "cases": {"<case id>": {<fields>}}}``
    with the same field names as ``reference`` in ``l2_cases.json``. Fields the
    native backend does not report are skipped, except next_state and
    fired_transition, which are required.
    """
    pack = json.loads((pack_dir / "l2_cases.json").read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if results.get("ir_hash") != pack["ir_hash"]:
        problems.append("ir_hash differs from the conformance pack")
    got_cases = results.get("cases") or {}
    for case in pack["cases"]:
        got = got_cases.get(case["id"])
        if got is None:
            problems.append(f"{case['id']}: missing")
            continue
        for field in COMPARED_FIELDS:
            if field not in got:
                if field in ("next_state", "fired_transition"):
                    problems.append(f"{case['id']}: {field} not reported")
                continue
            wanted = case["reference"].get(field)
            if _clean(got[field]) != wanted:
                problems.append(f"{case['id']}: {field} expected {wanted!r}, got {got[field]!r}")
    return problems
