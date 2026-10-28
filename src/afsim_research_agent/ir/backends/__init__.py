"""Deterministic backend compilers (design §24).

Plugin interface::

    compile(normalized_ir, resolved_context, catalog_versions) -> dict[relative_path, text]

Backends for specific simulation products are implemented outside this
repository, in environments authorized to use them, against the same interface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from afsim_research_agent.ir.backends import btcpp_xml, mermaid, python_sim

Compiler = Callable[[dict[str, Any], dict[str, Any], dict[str, str]], dict[str, str]]

COMPILERS: dict[str, tuple[Compiler, str]] = {
    "mermaid": (mermaid.compile, mermaid.TEMPLATE_VERSION),
    "btcpp_xml": (btcpp_xml.compile, btcpp_xml.TEMPLATE_VERSION),
    "python_sim": (python_sim.compile, python_sim.TEMPLATE_VERSION),
}


class CompileRefused(RuntimeError):
    """The compiler input does not satisfy the input guarantees."""


def check_input(ir: dict[str, Any], context: dict[str, Any]) -> None:
    from afsim_research_agent.ir.normalize import verify_hash

    if ir.get("draft"):
        raise CompileRefused(
            "normalized IR is a draft (unconfirmed items); confirm and re-normalize"
        )
    if not verify_hash(ir):
        raise CompileRefused("normalized IR content hash does not match ir_hash")
    if context.get("ir_hash") != ir["ir_hash"]:
        raise CompileRefused("resolved_context was produced for a different IR")


def compile_backend(name: str, ir: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    check_input(ir, context)
    compiler, _ = COMPILERS[name]
    return compiler(ir, context, dict(ir["catalog_versions"]))
