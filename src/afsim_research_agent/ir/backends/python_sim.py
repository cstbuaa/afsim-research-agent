"""Python backend for the built-in 2D simulation.

Generated role modules bind a role to the reference interpreter
(:mod:`afsim_research_agent.ir.runtime`), which is also what L2 tests use, so
L2 results and simulation behavior come from one set of semantics. The modules
pin the IR hash and component bindings; ``run_scenarios.py`` runs every L3
scenario of the package.
"""

from __future__ import annotations

import pprint
from typing import Any

from afsim_research_agent.ir.backends.common import header, trees_for_role

TEMPLATE_VERSION = "python_sim-2026.10.1"


def _role_module(ir: dict[str, Any], role: dict[str, Any], context: dict[str, Any]) -> str:
    binding = context["roles"][role["id"]]
    lines = [
        f'"""Controller for role {role["id"]} ({role["count"]} instance(s))."""',
        header("python_sim", ir, TEMPLATE_VERSION, "#").rstrip("\n"),
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from afsim_research_agent.ir.runtime import RoleController",
        "",
        f"ROLE_ID = {role['id']!r}",
        f"IR_HASH = {ir['ir_hash']!r}",
        f"INSTANCES = {role['count']}",
        f"PLATFORM_TEMPLATE = {binding['platform_template']!r}",
        "COMPONENTS: dict[str, Any] = "
        + pprint.pformat(binding["components"], width=88, sort_dicts=True),
        f"TREES = {trees_for_role(ir, role)!r}",
        "",
        "",
        "def make_controller(",
        "    ir: dict[str, Any], instance: int = 0, **kwargs: Any",
        ") -> RoleController:",
        '    """Create the controller for one instance; refuses a different IR."""',
        '    if ir.get("ir_hash") != IR_HASH:',
        '        raise RuntimeError("normalized IR does not match this generated module")',
        "    if not 0 <= instance < INSTANCES:",
        '        raise ValueError(f"instance must be in 0..{INSTANCES - 1}")',
        "    return RoleController(ir, ROLE_ID, instance, **kwargs)",
        "",
    ]
    return "\n".join(lines)


_RUNNER = '''"""Run every L3 scenario of this model package in the built-in 2D simulation."""
{header}
from __future__ import annotations

import json
import sys
from pathlib import Path

from afsim_research_agent.ir.normalize import verify_hash
from afsim_research_agent.ir.sim import run_scenarios

PACKAGE = Path(__file__).resolve().parents[2]
IR_HASH = {ir_hash!r}


def main() -> int:
    ir = json.loads((PACKAGE / "normalized_ir.json").read_text(encoding="utf-8"))
    if ir.get("ir_hash") != IR_HASH or not verify_hash(ir):
        print("normalized_ir.json does not match the generated code; rebuild the package")
        return 2
    contexts = json.loads((PACKAGE / "resolved_context.json").read_text(encoding="utf-8"))
    context = contexts["backends"]["python_sim"]
    results = run_scenarios(ir, context, package_dir=PACKAGE)
    for result in results:
        print(("PASS" if result.passed else "FAIL"), result.id)
        for item in result.expectations:
            mark = "ok " if item.passed else "BAD"
            print(
                f"  {{mark}} {{item.id}}: measured={{item.measured}} "
                f"threshold={{item.threshold}} {{item.detail}}"
            )
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def compile(
    ir: dict[str, Any], context: dict[str, Any], catalog_versions: dict[str, str]
) -> dict[str, str]:
    files = {f"roles/{role['id']}.py": _role_module(ir, role, context) for role in ir["roles"]}
    files["roles/__init__.py"] = '"""Generated role controllers."""\n'
    files["run_scenarios.py"] = _RUNNER.format(
        header=header("python_sim", ir, TEMPLATE_VERSION, "#").rstrip("\n"), ir_hash=ir["ir_hash"]
    )
    return files
