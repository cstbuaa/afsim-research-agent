"""Platform capability catalog: loading, lookup and feasibility judgment.

See design/capability-catalog-integration-v1.md. Only the formal (public) layer is read.
"""

from afsim_research_agent.capability.assess import (
    LABELS,
    RANK,
    Aliases,
    ElementMapping,
    Feasibility,
    RefResult,
    Verdict,
    assess,
    assess_ir_element,
    assess_ref,
    gap_id,
    map_binding,
    map_ir_element,
    summarize,
)
from afsim_research_agent.capability.catalog import (
    Capability,
    CapabilityCatalog,
    CatalogError,
    Interface,
    default_catalog_root,
)

__all__ = [
    "LABELS",
    "RANK",
    "Aliases",
    "Capability",
    "CapabilityCatalog",
    "CatalogError",
    "ElementMapping",
    "Feasibility",
    "Interface",
    "RefResult",
    "Verdict",
    "assess",
    "assess_ir_element",
    "assess_ref",
    "default_catalog_root",
    "gap_id",
    "map_binding",
    "map_ir_element",
    "summarize",
]
