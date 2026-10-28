"""The deep agent served by ``langgraph dev`` (see ``langgraph.json``).

Assembly lives in ``afsim_research_agent.orchestrator.graph.build_agent``; the LangGraph
server supplies the checkpointer, so none is passed here. For terminal use run
``python -m afsim_research_agent.orchestrator run``.
"""

from afsim_research_agent.orchestrator.graph import build_agent

agent = build_agent()
