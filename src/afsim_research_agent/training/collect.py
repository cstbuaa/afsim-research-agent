"""S0 (design ``training-data-construction-v1`` §3): historical tactic sheets re-checked with
the current checker. Each package (``tactic_sheet.md`` + ``falcon_request.yaml`` [+
``obligations.yaml``]) gets:

- ``valid``: every check passes (an injection base for repair data);
- ``fixable``: something fails and the repair loop would start on it (L1 fails, or a 2D
  failure the rule triage puts on the sheet): a real repair test case;
- ``outside``: what fails is not the sheet's (scenario / judge / capability): left out.

The sheets stay where they are (local); the index holds paths, statuses, the failed
checks and a structure fingerprint for de-duplication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.sheet.blocks import split_sheet
from afsim_research_agent.falcon.v3.draft import Drafter, V3Request, _NoModel
from afsim_research_agent.falcon.v3.fixer import SheetFixer
from afsim_research_agent.ir.v2 import Contracts

_CONTRACTS: dict[str, Any] = {}


def load(pkg: Path) -> Drafter:
    """The drafter the way ``compile_package`` builds it: the saved obligations, no model."""
    request = V3Request.load(pkg / "falcon_request.yaml")
    if request.obligations is None:
        saved = pkg / "obligations.yaml"
        data = yaml.safe_load(saved.read_text(encoding="utf-8")) if saved.exists() else {}
        request = request.model_copy(
            update={
                "obligations": (data or {}).get("obligations") or [],
                "extract_obligations": False,
            }
        )
    key = str(request.capability_set)
    if key not in _CONTRACTS:
        _CONTRACTS[key] = Contracts.load(request.capability_set)
    d = Drafter(request, _CONTRACTS[key], ModelRunner(_NoModel()))
    asyncio.run(d.obligations_stage())
    d.blocks = split_sheet((pkg / "tactic_sheet.md").read_text(encoding="utf-8"))[0]
    d.slices = d._slices(d.compile(pending=True))
    return d


def fingerprint(sheet: str) -> str:
    """States and transitions per role, numbers and names of tests dropped: two sheets
    with the same machine count as one structure."""
    keep = []
    for b in split_sheet(sheet)[0]:
        if b.kind == "mermaid":
            keep += sorted(
                re.sub(r"\s+", " ", x.strip()) for x in b.text.splitlines() if "-->" in x
            )
        if b.kind == "pytree":
            calls = re.findall(r"tactic\(\"([^\"]+)\"|(\w+\.\w+)\(", b.text)
            keep += sorted({a or c for a, c in calls})
    return hashlib.sha1("\n".join(map(str, keep)).encode()).hexdigest()[:12]


def recheck(pkg: Path) -> dict[str, Any]:
    d = load(pkg)
    fixer = SheetFixer(d)
    check = fixer.last
    failed = [f"L1:{p.render()[2:][:100]}" for p in check.problems[:5]]
    if check.l1_ok:
        failed += [f"L2:{t['id']}" for t in check.l2 if not t["passed"]]
        failed += [f"L3:{s['id']}" for s in check.l3 if not s["passed"]]
    if check.verified:
        status = "valid"
    elif fixer.outside_left():
        status = "outside"
    else:
        status = "fixable"
    found = fixer.findings() if check.l1_ok and not check.verified else []
    sheet = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    return {
        "pkg": str(pkg),
        "status": status,
        "l1": bool(check.l1_ok),
        "score": list(fixer.score(check)),
        "failed": failed,
        "findings": [f"{f.category}:{f.symptom}" for f in found],
        "fix_log": (pkg / "fix_log.json").exists(),
        "requirement": hashlib.sha1(str(d.r.requirement).encode()).hexdigest()[:10],
        "structure": fingerprint(sheet),
    }


def scan(roots: list[Path]) -> list[Path]:
    out = []
    for root in roots:
        for sheet in sorted(root.rglob("tactic_sheet.md")):
            pkg = sheet.parent
            if (pkg / "falcon_request.yaml").exists() and "training" not in pkg.parts:
                out.append(pkg)
    return out


def main(roots: list[str], out: str) -> None:
    rows = []
    for pkg in scan([Path(r) for r in roots]):
        try:
            rows.append(recheck(pkg))
        except Exception as exc:  # noqa: BLE001 - an old package the loader cannot read
            rows.append({"pkg": str(pkg), "status": "error", "error": repr(exc)[:300]})
    Path(out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
