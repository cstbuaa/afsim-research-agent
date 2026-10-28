"""Tactical Falcon: requirement + Crow evidence -> behavior_spec.yaml draft.

Design: docs/design/tactical-falcon-v1.md. The language model only proposes;
every constraint that can be checked mechanically is enforced in code after
the model answers.
"""

from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.llm import FalconModel, ScriptedModel

__all__ = ["FalconError", "FalconModel", "ScriptedModel"]
