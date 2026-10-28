# Research Source Collection Plan

## Scope

Turn Crow-generated search queries and approved local roots into a traceable set
of `PaperQADocument` objects. This stage collects sources; it does not synthesize
tactical behavior.

### Task 1: Safe PDF acquisition

**Files:**
- Create: `src/afsim_research_agent/web/pdf.py`
- Modify: `src/afsim_research_agent/web/__init__.py`
- Test: `tests/unit/web/test_pdf.py`

- [ ] Validate and pin DNS for every request and redirect.
- [ ] Accept only PDF content, enforce a byte limit, hash the bytes, and use a
      content-addressed filename.

### Task 2: Unified web and local collection

**Files:**
- Create: `src/afsim_research_agent/literature/collection.py`
- Modify: `src/afsim_research_agent/literature/models.py`
- Modify: `src/afsim_research_agent/literature/__init__.py`
- Test: `tests/unit/literature/test_collection.py`

- [ ] Search each query and deduplicate URLs.
- [ ] Convert extracted webpages to Markdown documents and retain provenance.
- [ ] Discover PaperQA-native local files and materialize code as text snapshots.
- [ ] Return documents, source records, and bounded failure records.

### Task 3: Documentation and verification

**Files:**
- Modify: `README.md`

- [ ] Document the Crow-to-collection-to-PaperQA boundary.
- [ ] Run focused tests, complete tests, Ruff, dependency checks, and a live
      collection smoke test.
