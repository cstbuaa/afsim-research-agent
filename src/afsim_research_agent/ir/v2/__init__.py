"""Tactical Behavior IR 2.0: seven layers over the capability set (动作集 / 战术集).

See docs/design/tactical-behavior-ir-v2-layered.md.
"""

from afsim_research_agent.ir.v2.contracts import Contracts
from afsim_research_agent.ir.v2.from_tactic import TacticConversionError, mission_to_ir
from afsim_research_agent.ir.v2.models import SCHEMA_VERSION, BehaviorSpecV2, json_schema
from afsim_research_agent.ir.v2.normalize import Result, normalize

__all__ = [
    "SCHEMA_VERSION",
    "BehaviorSpecV2",
    "Contracts",
    "Result",
    "TacticConversionError",
    "json_schema",
    "mission_to_ir",
    "normalize",
]
