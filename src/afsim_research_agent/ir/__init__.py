"""Tactical Behavior IR 1.0: models, validation, normalization, tests and backends.

The IR is backend neutral. Nothing in this package knows about any specific
simulation product; concrete component bindings live in ``resolved_context.json``
and in the backend packages under :mod:`afsim_research_agent.ir.backends`.
"""

from afsim_research_agent.ir.diagnostics import Diagnostic, Severity
from afsim_research_agent.ir.loader import LoadResult, load_spec, load_spec_text

IR_SCHEMA_VERSION = "1.0"

__all__ = [
    "IR_SCHEMA_VERSION",
    "Diagnostic",
    "LoadResult",
    "Severity",
    "load_spec",
    "load_spec_text",
]
