# PaperQA2 Migration Plan

## Scope

Add a local Ollama-backed PaperQA2 evidence-query boundary without attaching it
to the main Deep Agent yet. Tavily discovery and webpage reading remain unchanged.

### Task 1: Configuration and dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `src/afsim_research_agent/config.py`
- Test: `tests/unit/literature/test_paperqa_backend.py`

- [ ] Pin `paper-qa[pymupdf]==2026.8.12`.
- [ ] Expose evidence count, source count, concurrency, embedding dimension, and
      Qwen thinking control through application settings.
- [ ] Verify the generated PaperQA settings target local Ollama and do not query
      external metadata for local documents.

### Task 2: PaperQA adapter

**Files:**
- Create: `src/afsim_research_agent/literature/models.py`
- Create: `src/afsim_research_agent/literature/paperqa_backend.py`
- Create: `src/afsim_research_agent/literature/__init__.py`
- Test: `tests/unit/literature/test_paperqa_backend.py`

- [ ] Add a document request contract with local path, citation, title, and DOI.
- [ ] Add an asynchronous query method using `Docs.aadd` and `Docs.aquery`.
- [ ] Normalize answers and evidence into project-owned Pydantic models.
- [ ] Reject blank questions and empty document sets before invoking PaperQA.

### Task 3: Operator documentation and verification

**Files:**
- Modify: `README.md`

- [ ] Document the local Ollama models, environment values, and manual call.
- [ ] Run focused tests, the complete suite, Ruff, `pip check`, and a live Ollama
      smoke test from the target project.
