# Research Foundation Design

## Scope

The first development line provides three independently testable capabilities:

1. internet search and webpage reading;
2. hybrid retrieval over a local knowledge base;
3. deterministic source-quality evaluation and evidence merging.

Behavior-model generation, behavior trees, processor rendering, and direct AFSIM integration are outside this stage.

## Design principles

- Keep orchestration separate from tool implementation.
- Give every stage typed input and output records.
- Preserve raw evidence and record content hashes.
- Treat model output as candidate interpretation, not ground truth.
- Keep conflicting evidence instead of averaging it away.
- Make indexes rebuildable from immutable raw inputs.
- Store a manifest for every research run.

## Stage 1: web tools

`search_web` discovers candidate URLs. `read_webpage` retrieves one selected URL, validates every redirect, extracts the main content, assigns stable paragraph identifiers, and stores a snapshot. Search snippets are discovery metadata and are not final evidence.

Acceptance requires deterministic provider fixtures, structured errors, URL safety tests, bounded output, and source provenance in every result.

## Stage 2: local hybrid retrieval

Ingestion produces stable chunks from local documents and code. SQLite FTS5 supplies lexical candidates. A dedicated Ollama embedding model supplies semantic candidates. Reciprocal rank fusion combines ranks while retaining both component ranks.

Acceptance requires a fixed query set with expected relevant chunks, repeatable indexing, deletion/rebuild behavior, and explicit reporting when the embedding service is unavailable.

## Stage 3: evidence processing

Quality scoring uses configured rules based on source kind, directness, recency, authorship, and corroboration. Evidence records bind a normalized claim to an exact excerpt and location. Deduplication operates at canonical URL, content hash, and claim levels. Merging groups support and conflict without deleting minority evidence.

Acceptance requires fixtures for duplicate pages, mirrored content, corroborating sources, contradictory sources, weak sources, and missing dates.

## Stage 4: Deep Agents integration

Only accepted tools are attached to the main Deep Agent. The initial implementation uses one local Ollama-backed agent and no subagents. Tool-call budgets and result-size limits remain configuration owned by the application.

Acceptance requires an end-to-end run that produces a stored manifest, source snapshots, retrieval candidates, merged evidence, and a cited answer.

## Review points

The operator reviews provider choice before Stage 1, embedding model and retrieval evaluation data before Stage 2, and source-quality policy weights before Stage 3. Each gate can be rerun independently before proceeding.
