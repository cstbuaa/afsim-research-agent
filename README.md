# AFSIM Research Agent

This example is the research and retrieval foundation for an AFSIM behavior-modeling agent. The first development stage intentionally covers only:

1. web search and webpage reading;
2. local hybrid retrieval;
3. source-quality evaluation and evidence merging.

Behavior generation and AFSIM integration will consume these capabilities later.

## Control model

Each stage has an explicit input and output model. Raw sources, indexes, and run records are stored separately so a result can be inspected and reproduced.

```text
query
  -> web search -> webpage reader ----\
                                      -> evidence scoring -> evidence merge
  -> local lexical + semantic search -/
```

The language model may plan searches and extract candidate claims. Deterministic code owns fetching policy, retrieval fusion, source scoring, deduplication, conflict flags, and run records.

## Layout

```text
src/afsim_research_agent/
  agent.py          Deep agent served by `langgraph dev` (built by orchestrator/)
  orchestrator/     Deep Agents wiring: stage tools, workspace policy, extension points, CLI
  config.py         Environment-backed runtime settings
  crow/             Requirement planning and research orchestration
  models.py         Shared typed data contracts
  web/              Search, reading, and URL security
  knowledge/        Ingestion and hybrid local retrieval
  evidence/         Quality, deduplication, and merging
data/
  raw/              Immutable source snapshots
  index/            Rebuildable local indexes
  runs/             Per-run manifests and outputs
tests/
  unit/             Network-free deterministic tests
docs/superpowers/specs/
  ...               Approved design and acceptance gates
```

## Local setup

Requirements: Python 3.11+, `uv`, and an Alibaba Cloud Model Studio API key.

```bash
cp .env.example .env
uv sync --group dev
uv run langgraph dev
```

The agent is assembled in `orchestrator/graph.py`; its tools wrap the Crow, Falcon and IR
stages, and tools that change decisions or run external platforms pause for approval.
Terminal use and checks:

```bash
python -m afsim_research_agent.orchestrator tools                 # tools and extension status
python -m afsim_research_agent.orchestrator check --package <pkg> # offline wiring check, no API key
python -m afsim_research_agent.orchestrator probe --package <pkg> # live function-calling probe (qwen)
python -m afsim_research_agent.orchestrator run "<需求>"           # interactive run
```

See `docs/design/agent-orchestrator-p1.md` for the extension interfaces.

## Stage 1A: webpage reader

Stage 1A is implemented. It validates and pins public HTTP/HTTPS destinations,
revalidates redirects, limits response size, extracts the main content with
Trafilatura, assigns stable paragraph identifiers, and can persist the raw HTML
and normalized document.

```python
from pathlib import Path

from afsim_research_agent.web import read_webpage

document = read_webpage(
    "https://example.com/article",
    raw_dir=Path("data/raw"),
)
print(document.source.uri, document.paragraph_ids)
```

The reader raises `WebReadError` for network, HTTP, content-type, size, redirect,
and extraction failures. Unsafe URLs raise `URLValidationError` before a request
is sent.

## PaperQA2 evidence queries

PaperQA2 is available as an independent evidence-query boundary. It uses
Alibaba Cloud Model Studio's `qwen3.8-flash` chat model and
`text-embedding-v4` embedding model through the OpenAI-compatible API. The
Tactical Crow pipeline invokes it after source collection.

```python
import asyncio
from pathlib import Path

from afsim_research_agent.literature import PaperQABackend, PaperQADocument


async def main() -> None:
    report = await PaperQABackend().query(
        "What conditions trigger the formation transition?",
        [
            PaperQADocument(
                path=Path("papers/formation-control.pdf"),
                citation="Author. Formation Control. 2026.",
                title="Formation Control",
            )
        ],
    )
    print(report.answer)
    for item in report.evidence:
        print(item.citation, item.location, item.score)


asyncio.run(main())
```

For local and internal documents, external metadata lookup and multimodal
enrichment are disabled. `PAPERQA_DISABLE_THINKING=true` keeps planning and
evidence extraction in non-thinking mode for predictable structured output.

## Alibaba Cloud Model Studio configuration

Copy `.env.example` to `.env`, then put the API key issued by Alibaba Cloud
Model Studio in the first variable below. `.env` is ignored by Git and must not
be committed.

```dotenv
DASHSCOPE_API_KEY=sk-your-real-api-key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_CHAT_MODEL=qwen3.8-flash
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
```

The planner, Deep Agent, PaperQA2 summaries and PaperQA2 final answers share
the configured chat model. PaperQA2 uses the configured embedding model with a
1024-dimensional index.

## Stage 1B: Tavily search

Stage 1B is implemented as a separate discovery step. Configure the key in the
process environment, then call `search_web`:

```bash
export TAVILY_API_KEY="your-key"
```

```python
from afsim_research_agent.web import search_web

hits = search_web("multi-agent formation tactics", limit=5)
for hit in hits:
    print(hit.title, hit.url, hit.published_at)
```

The result count is limited to 1–10. Tavily answer generation and raw-content
retrieval are disabled, so the provider returns only URL candidates and short
discovery snippets. A selected URL must pass through `read_webpage` before its
content is used as evidence. Provider scores remain provider metadata and are
not treated as source-quality scores. Missing configuration, provider failures,
and malformed responses raise `SearchError`.

## Research source collection

Collection is done by `SourceCollector`, which keeps deduplication state and
budgets across every round of a run. Each round has five deterministic stages,
and every search result is recorded in a ledger (`SourceCollection.candidates`):

1. **Discovery** – channels run the planner's query families:
   - `web` – Tavily, general web;
   - `doctrine` – Tavily restricted to official/doctrine domains;
   - `technical_report` – Tavily restricted to DTIC, NPS Calhoun, AFIT, RAND,
     CNA, MITRE, NASA NTRS, NATO STO, FOI;
   - `scholarly` – OpenAlex, Semantic Scholar, arXiv and CORE (CORE only with a key);
   - `library` – your local library (`data/library/`, e.g. CNKI papers).
2. **Triage** – dedup by normalized URL, DOI and normalized title (doctrine
   editions are kept apart); DOIs are verified against OpenAlex (free lookup;
   wrong DOIs are detached, retractions rejected); superseded doctrine editions
   are flagged; blocked hosts and closed-access works are rejected; rule-based
   source quality (tiers A–D) is scored.
3. **Screening** – the chat model scores each candidate's title/abstract 0–3
   against the research questions (`literature/screening.py`); 0 is dropped,
   the score is blended into the fetch priority (0.45 quality + 0.40 screening +
   0.15 provider rank).
4. **Acquisition** – fetch in priority order until `max_total_sources`; the
   response type is sniffed, PDFs are stored by SHA-256, HTML is snapshotted,
   mirrors are collapsed by content hash.
5. **Citation snowballing** – references and citations of the best accepted
   scholarly sources (Semantic Scholar with a key, otherwise OpenAlex) go
   through the same triage → screening → acquisition path.

Non-accepted candidates carry a reason (`http_error`, `dns_error`,
`security_rejected`, `timeout`, `invalid_pdf`, `no_extractable_text`,
`not_open_access`, `retracted`, `blocked_domain`, `screened_out`, …).

### Domain vocabulary and doctrine lineage

`src/afsim_research_agent/domain/data/tactical_vocabulary.toml` maps tactical
concepts (formations, movement techniques, unit types, MUM-T, task allocation,
TEWA, terrain, behavior trees, CGF, AFSIM) to English/Chinese synonyms, research
terms and doctrine publications. The planner receives the matched entries as a
glossary and the vocabulary adds deterministic scholarly, doctrine and Chinese
queries. `doctrine_supersession.toml` records which manuals replace which
(`verified = false` entries still need checking); superseded designations are
searched under their successor and flagged in reports.

### Local library and Chinese literature

CNKI/Wanfang have no public API. Crow therefore writes Chinese search strings
with direct CNKI/Wanfang links into the report; save the papers you choose into
`data/library/` (optionally with a `<file>.meta.toml` sidecar, see
`data/library/README.md`) and they are screened and used in later runs.

### Network notes (proxies)

Shell `HTTP(S)_PROXY` variables are ignored on purpose. To route downloads
through a local proxy set `CROW_HTTP_PROXY=http://127.0.0.1:7890`. If your
proxy runs in TUN/fake-IP mode, DNS answers come from `198.18.0.0/15` and are
rejected by the SSRF guard (`security_rejected`); allow exactly that range with
`CROW_ALLOWED_PROXY_CIDRS=198.18.0.0/15`. Set `CROW_CONTACT_EMAIL` so publishers
such as Wikimedia and OpenAlex can identify the client.

## Tactical Crow research stage

`CrowResearchPipeline` accepts a natural-language tactical requirement. The
configured cloud model first turns it into bounded research questions and
academic search queries. The pipeline then collects internet and local sources
and asks PaperQA2 to produce a cited report for each research question.

```python
import asyncio
from pathlib import Path

from afsim_research_agent.crow import CrowResearchPipeline


async def main() -> None:
    result = await CrowResearchPipeline().run(
        "研究四机有人/无人协同编队在威胁接近时的队形转换与协同机制",
        local_roots=[Path("knowledge")],
        output_dir=Path("data/runs/crow-research"),
        max_web_results=3,
    )

    print(result.plan.search_queries)
    for source in result.collection.sources:
        print(source.title, source.uri)
    for report in result.reports:
        print(report.question, report.answer)
        for evidence in report.evidence:
            print(evidence.citation, evidence.location, evidence.score)


asyncio.run(main())
```

The returned `CrowResearchResult` is an inspectable research package containing
the interpreted requirement, search plan, the candidate ledger, accepted sources
with quality tiers and bibliographic metadata, categorized failures, cited
answers whose evidence links back to a `source_id`, a run manifest (models,
prompt and rule versions, dependency versions, Git commit, stage timings) and a
Falcon readiness assessment.

PaperQA2 parses and embeds the collected documents **once per run**
(`PaperQABackend.build_corpus`) and every research question is answered against
that corpus (`query_corpus`). A document PaperQA2 cannot parse is recorded in
`ingest_failures` instead of aborting the run.

`crow/readiness.py` applies an explicit gate before anything is handed to
Tactical Falcon: at least 3 usable sources, at least 2 A/B-tier sources, at least
75 % of questions with two or more evidence excerpts scoring ≥ 6/10, at least
half of strong evidence from A/B sources, and at most 25 % of answers declaring
insufficient information.

### Independent internet-only test

Run the command from the project root. It loads the existing `.env`, asks for
one natural-language requirement, searches only the internet, downloads the
selected PDFs or webpage text, and then runs PaperQA2 with the configured
Alibaba Cloud models.

```bash
conda activate afsim_agent
python -m afsim_research_agent.crow.cli
```

Example input:

```text
研究四机有人/无人协同编队在威胁接近时，如何触发防御队形转换并协调各平台行为
```

The requirement can also be supplied without the interactive prompt:

```bash
python -m afsim_research_agent.crow.cli \
  --requirement "研究四机有人/无人协同编队的防御队形转换" \
  --max-web-results 2 --max-sources 12 --library data/library --gap-rounds 1
```

`--max-web-results` is the number of candidates each query returns;
`--max-sources` (default `CROW_MAX_SOURCES`, 12) caps how many sources the run
accepts. Every run gets a timestamped directory under `data/runs/crow-research/`:

```text
data/runs/crow-research/<timestamp>/
  pdf/                    downloaded documents, named by SHA-256
  web/                    extracted webpage text given to PaperQA2
  raw/                    original HTML snapshots + normalized JSON
  research_report.md      human-readable review report
  research_result.json    complete structured result
```

The Markdown report starts with the Falcon gate verdict and per-question
coverage, then the plan, accepted sources with quality reasons, categorized
failures, a manual-acquisition worklist (A/B candidates that could not be
fetched), the cited answers, the full candidate ledger and the run manifest.

### Follow-up rounds

After round 1, research questions with fewer than two strong evidence excerpts
(or an answer declaring missing information) get one bounded follow-up round
(`CROW_GAP_ROUNDS`, `CROW_GAP_ROUND_SOURCES`): the planner writes new,
differently-worded queries for exactly those questions, new sources are added
to the same PaperQA corpus, and only the weak questions are re-answered.

### Retrieval evaluation

`eval/retrieval_gold.toml` holds typical requirements with the sources a good
search must find (draft — review it). The harness measures recall at three
stages (discovered → kept after screening → accepted) and, with your relevance
labels, precision of accepted sources:

```bash
python -m afsim_research_agent.evaluation.retrieval verify      # check gold DOIs
python -m afsim_research_agent.evaluation.retrieval run         # all cases, ~1-2 min each
python -m afsim_research_agent.evaluation.retrieval rescore --run-dir data/eval/<run> \
    --labels data/eval/<run>/precision_labels.csv
```

Run it before and after any retrieval change and compare `evaluation.json`.

### Tests

```bash
pytest -q                      # network-free unit tests (default)
ruff check src tests
pytest -m live -s tests/live   # opt-in: real Tavily/OpenAlex/DashScope calls
```

## Tactical Behavior IR (Falcon output contract)

`src/afsim_research_agent/ir/` implements the backend-neutral Tactical Behavior IR
1.0 (design: `docs/design/tactical-behavior-ir-v1.md`). A model package is a
directory holding `behavior_spec.yaml` (authoritative, user-editable) and
optionally `evidence_package.json` (Crow snapshot) and `bindings/<backend>.json`.

```bash
python -m afsim_research_agent.ir validate examples/armor_platoon_terrain_formation  # L1
python -m afsim_research_agent.ir test     examples/armor_platoon_terrain_formation  # L1 + gate + L2
python -m afsim_research_agent.ir build    examples/armor_platoon_terrain_formation  # everything
python -m afsim_research_agent.ir build <pkg> --draft    # skip the confirmation gate; no code
python -m afsim_research_agent.ir simulate <pkg> --scenario open_to_urban_transition
python -m afsim_research_agent.ir schema --out behavior_spec.schema.json
python -m afsim_research_agent.ir cosim <pkg> --engine reference            # co-simulation contract check
python -m afsim_research_agent.ir cosim <pkg> --engine path/adapter.py:make_engine --trace-dir runs/
python -m afsim_research_agent.ir judge <pkg> --scenario ID --trace runs/ID.jsonl
```

External engines (e.g. a lab simulator) connect through `ir/cosim/`: one adapter
class (`reset/apply_commands/send_messages/apply_event/step/close`) and
`bindings/<engine>.json`. See `integrations/lab_engine/README.md`.

Engines on another machine speak the `tbe-step/1` protocol
(`docs/design/engine-step-protocol-v1.md`): `--engine tcp://host:port`. A
dependency-free C++17 engine host skeleton lives in `integrations/engine_bridge_cpp/`;
check any host with `python -m afsim_research_agent.ir protocol-check --host H --port P`.
Native backends follow `docs/design/backend-developer-guide.md` and verify with
`python -m afsim_research_agent.ir conformance export|compare`.

`build` writes only derived files into the package: `normalized_ir.json`,
`resolved_context.json`, `validation_report.json`, `review.md` and
`generated/{mermaid,btcpp_xml,python_sim}/` plus `generated/manifest.json`. It
never edits `behavior_spec.yaml`, refuses to touch a `generated/` directory it
did not create, and deletes its own stale generated code when a build fails.

Pipeline: strict YAML load -> Pydantic schema (IR002) -> semantic validation
(references, units, types, trees, state machines, resources, evidence basis,
roles, formations, coordination, tests, scenarios) -> confirmation gate (every
pending basis/assumption blocks) -> normalization (canonical units, expanded
coordination patterns, sha256 hash) -> L2 single-tick tests on the reference
interpreter -> backend binding and compilation -> L3 scenarios in the built-in
2D simulation. The example's numbers are illustrative placeholders, not doctrine.

## Tactical Falcon (requirement + evidence -> IR draft)

`src/afsim_research_agent/falcon/` (design: `docs/design/tactical-falcon-v1.md`)
turns a request file plus a Crow `research_result.json` into a new model package.
The model only proposes; quote checks, evidence IDs, conflict detection and
`basis` rules are enforced in code. Falcon refuses to run when the Crow gate did
not pass, and never overwrites an existing package.

```bash
python -m afsim_research_agent.falcon draft falcon_request.yaml   # -> models/<model_id>/
#   edit models/<model_id>/confirmations.yaml: decision confirm | reject | override
python -m afsim_research_agent.falcon confirm models/<model_id> --by <name>
python -m afsim_research_agent.falcon status  models/<model_id>
python -m afsim_research_agent.ir build models/<model_id>
```

A draft package holds `falcon_request.yaml`, `evidence_package.json`,
`behavior_spec.yaml`, `confirmations.yaml`, `falcon_review.md`,
`falcon_trace.json` and `validation_report.json`. `FALCON_CHAT_MODEL` overrides
the DashScope chat model; `falcon evaluate <pkg> [--reference PKG]` writes
`falcon_eval.json`. Online evaluation steps: `docs/runbooks/falcon-online-eval.md`.

## Development gates

- Gate 1: web search and webpage reading return structured, auditable records.
- Gate 2: local lexical and semantic retrieval pass a fixed evaluation set.
- Gate 3: quality scoring, deduplication, and conflict handling are deterministic.
- Gate 4: the tools are attached to Deep Agents and exercised with Alibaba Cloud
  Model Studio.

See the design document under `docs/superpowers/specs/` for acceptance criteria.
