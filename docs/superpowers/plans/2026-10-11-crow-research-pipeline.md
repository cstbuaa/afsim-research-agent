# Tactical Crow Research Pipeline Plan

## Scope

Accept one natural-language tactical requirement, plan evidence questions and
search queries, collect approved internet and local sources, and run PaperQA2
for each research question. Behavior-model synthesis remains the next stage.

### Task 1: Requirement planner

**Files:**
- Create: `src/afsim_research_agent/crow/models.py`
- Create: `src/afsim_research_agent/crow/planner.py`
- Test: `tests/unit/crow/test_planner.py`

- [ ] Use local ChatOllama structured output with reasoning disabled.
- [ ] Produce a requirement summary, research questions, search queries, and key terms.
- [ ] Reject blank requirements before model invocation.

### Task 2: Research pipeline

**Files:**
- Create: `src/afsim_research_agent/crow/research.py`
- Create: `src/afsim_research_agent/crow/__init__.py`
- Test: `tests/unit/crow/test_research.py`

- [ ] Run source collection from the approved plan and local roots.
- [ ] Query PaperQA2 sequentially for every research question.
- [ ] Return the plan, source collection, and reports as one project-owned result.
- [ ] Preserve an empty-source result for operator inspection without invoking PaperQA2.

### Task 3: Documentation and verification

**Files:**
- Modify: `README.md`

- [ ] Document one-call usage and current boundary.
- [ ] Run focused tests, complete tests, Ruff, and a live local-Ollama smoke test.
