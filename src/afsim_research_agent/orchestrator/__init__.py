"""Deep Agents orchestration layer (agent-loop-deepagents-v1 §3, P1 skeleton).

Layout:

- ``results``    — ``StageResult``: what every stage returns to the agent (a short summary;
                   full artifacts stay on disk).
- ``workspace``  — where the agent may read and write; refuses to overwrite existing files.
- ``stages``     — plain-Python wrappers around Crow / Falcon / IR / backends. No dependency
                   on Deep Agents, so they are testable on their own and callable from scripts.
- ``extensions`` — interfaces and registry for capabilities under development
                   (action library, tactic library, plan→behavior-tree generator,
                   platform backends such as AFSIM, capability-plugin builders).
- ``tools``      — the stages and extensions exposed as LangChain tools.
- ``graph``      — ``build_agent()``: assembles the deep agent (tools, filesystem backend,
                   permissions, human-in-the-loop, checkpointer).
- ``cli``        — ``python -m afsim_research_agent.orchestrator``.

Only ``tools``, ``graph`` and ``cli`` import LangChain / Deep Agents.
"""

from afsim_research_agent.orchestrator.results import StageResult

__all__ = ["StageResult"]
