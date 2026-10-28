"""Built-in 2D simulation for L3 closed-loop scenario tests (design §21)."""

from afsim_research_agent.ir.sim.l3 import ScenarioResult, run_scenario, run_scenarios
from afsim_research_agent.ir.sim.world import SimWorld

__all__ = ["ScenarioResult", "SimWorld", "run_scenario", "run_scenarios"]
