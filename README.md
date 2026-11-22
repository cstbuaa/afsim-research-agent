# AFSIM Research Agent

**English** | [简体中文](README.zh-CN.md)

An LLM agent that turns a natural-language tactical requirement into a behavior model that
runs in AFSIM. The system researches the requirement, proposes complete tactics, writes the
behavior model, checks and repairs it automatically, and runs it in AFSIM with a replay. The
current research object is the armored platoon (ground vehicles: maneuver, formation,
perception, engagement, target sharing).

```text
natural-language requirement
  -> Crow     research: questions -> web / literature search -> cited evidence
              -> candidate tactics -> red-team review (a different model) -> selected tactic
  -> Falcon   modeling: tactic sheet (state machines + Python-style behavior trees + tests)
              drafted from verified skeletons or from scratch, only with the capability set
  -> IR 2.0   layered behavior model -> checks L1 / L2 / L3 + requirement obligations
  -> repair   ReAct repair loop (about 30 tool actions) until the checks pass
  -> AFSIM    scenario + run configuration -> mission run -> judgement -> .aer replay
```

The language model proposes; deterministic code owns everything that can be checked:
capability references, compilation to AFSIM, scenario binding, judgement, file safety.

## Status (2026-11)

| Part | State |
| --- | --- |
| Crow research and tactic proposal | Working; evidence gate, red-team review, tactic selection |
| Falcon v3 drafting | Working; skeleton drafting (formation, strike, formation change) and free drafting |
| Checks | L1 static rules, L2 single-tick tests, L3 2D multi-vehicle simulation with a team model, effect obligations (`destroys`, `formation_shape`, ...) |
| Repair loop | Working; structured tools, bypass checks, AFSIM in the loop |
| AFSIM backend | Working; advanced behavior tree output, scenario generation per model, branch scenarios, replays |
| Workbench | Working; LangGraph agent + web frontend, human approval, replay panel |
| Small-model post-training | Data generation and SFT done (Qwen3-8B QLoRA); GRPO reward written, training not run yet |

Selected results (details in `docs/handoff/`):

- Formation requirements, skeleton drafting vs. free drafting: AFSIM pass 5/10 vs. 0/10.
- Models passing the 2D checks pass AFSIM 86% of the time (after adding the multi-vehicle
  engagement and communication model; 60% before).
- Repair loop with a general LLM on 100 real failed sheets: 84.8% of the sheets failing L1
  reach L1 pass; fully repaired sheets pass AFSIM 12/14.
- SFT repair model on held-out injected errors: repair-step success 0.3% -> 91.1% (the
  injected errors are template-like; a real-failure test set is used for generalization).

## Layout

```text
src/afsim_research_agent/
  agent.py          Deep agent served by `langgraph dev` (built in orchestrator/)
  orchestrator/     Stage tools, workspace policy, human approval, workbench HTTP app, CLI
  crow/             Requirement planning, research, tactic proposal, red team, selection
  web/ literature/  Search, webpage reading, PaperQA2 evidence queries
  knowledge/ evidence/ domain/   Local retrieval, source quality, tactical vocabulary
  capability/       Capability catalog: lab export, groups, constraints
  falcon/v3/        Tactic sheet drafting, skeletons, prompts, repair loop and tools, triage
  ir/v2/            IR 2.0: models, normalization, obligations, effects, 2D simulation,
                    AFSIM compiler, scenario interface and evaluation
  replay/           AFSIM .aer replay building
  training/         Skeleton data generators, back-translation, error injection,
                    sample rendering, scoring, real-sheet evaluation, RL reward
  ir/ falcon/       IR 1.0 and Falcon v1 (earlier contract, kept for reference)
frontend/           deep-agents-ui based workbench frontend
scripts/            Environment checks, workbench start template, training scripts
docs/               design/, handoff/, analysis/, runbooks/
tests/unit/         Network-free deterministic tests
```

## Setup

Requirements: Python 3.11+, `uv` (or a conda env), an Alibaba Cloud Model Studio API key,
Node.js for the frontend, and an authorized AFSIM installation for AFSIM runs.

```bash
cp .env.example .env          # put the API key in .env; never commit it
uv sync --group dev
```

```dotenv
DASHSCOPE_API_KEY=sk-your-real-api-key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_CHAT_MODEL=qwen3.8-flash
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
```

Other settings: `TAVILY_API_KEY` (web search), `ORCHESTRATOR_MODEL` (orchestrator model),
`FALCON_CHAT_MODEL` (drafting and repair), `FALCON_AFSIM_BINDING`, `FALCON_AFSIM_KIT`,
`AFSIM_MISSION` (AFSIM runs), `CROW_HTTP_PROXY`, `CROW_CONTACT_EMAIL`.

### Capability set and local data

The capability set is exported from the lab's AFSIM project and stays on the local machine;
it is not in this repository. Local bindings, scenario kits, run outputs and training data
live under `_local/` and `data/`, which are not committed. Generated packages are always
written to new directories and never overwrite existing AFSIM files.

## Workbench

```bash
uv run langgraph dev --n-jobs-per-worker 2      # agent on 127.0.0.1:2024, assistant "afsim_research"
cd frontend && yarn install && yarn dev        # frontend on http://127.0.0.1:3000
```

On Windows, copy `scripts/start_workbench.example.ps1` to `_local/workbench/`, fill in the
local paths and run it. The agent calls stage tools (research, proposal, selection, drafting,
checks, repair, scenario setting, 2D simulation, AFSIM run); tools that change decisions or
run external platforms pause for approval (`ask_human` uses LangGraph `interrupt`). The
replay panel lists AFSIM runs and their `.aer` recordings.

Terminal use:

```bash
python -m afsim_research_agent.orchestrator tools                  # tools and extension status
python -m afsim_research_agent.orchestrator check --package <pkg>  # offline wiring check
python -m afsim_research_agent.orchestrator run "<requirement>"     # interactive run
```

## Crow: research and tactics

```bash
python -m afsim_research_agent.crow.cli --requirement "<requirement>" \
  --max-web-results 2 --max-sources 12 --library data/library --gap-rounds 1
```

- Research questions are planned along situation -> action -> exception -> evidence.
- Sources: Tavily (web, doctrine domains, technical-report domains), OpenAlex, Semantic
  Scholar, arXiv, CORE, and a local library (`data/library/`, e.g. CNKI papers saved by hand).
- Each round: discovery -> triage (dedup, DOI verification, retractions, quality tiers A–D)
  -> LLM screening -> acquisition -> citation snowballing; every candidate is kept in a ledger
  with a rejection reason.
- PaperQA2 answers each question against the run's corpus with citations; weak questions get
  a bounded follow-up round.
- A readiness gate (enough A/B sources and strong evidence) runs before anything goes to
  Falcon.
- Tactic proposal produces complete tactics (roles, geometry, steps with end conditions,
  exceptions, decision authority, required capabilities), retrieved or transferred, reviewed
  by a different model. Outputs: `mission_profile.yaml`, `evidence_package.json`,
  `tactic_selection.yaml`, `capability_gaps.yaml`.

Run directories: `data/runs/crow-research/<timestamp>/`.

## Falcon v3: tactic sheet -> IR 2.0

```bash
python -m afsim_research_agent.falcon crow-request <tactics dir> --model-id <id>  # request from a Crow run
python -m afsim_research_agent.falcon draft3 <request.yaml> [--afsim-run <run cfg>]
python -m afsim_research_agent.falcon fix <package> --max-steps 30 --with-l3      # repair a copy
```

- A tactic sheet holds a `model` block (roles, events), one Mermaid state machine and one
  Python-style behavior-tree block per role, conditions, and a `verification` block (L2 tests,
  L3 scenarios). It compiles to a layered IR 2.0 package.
- **Skeleton drafting** (default): the model only answers a choice (skeleton, module switches,
  values the requirement states, role mapping, clauses left out); the sheet is generated by
  code from verified skeletons. Families: formation (K1–K6), strike (allocate / validate /
  select / fire / observe with failure exits, re-attack, engagement limit, cease order,
  destroy), formation change (column, line, wedges, echelons).
- **Obligations** taken from the requirement are judged on the 2D and AFSIM traces: response
  to cancel, advance, keep-off distance, precedence (stop before radar off), `destroys`
  (count, roles, time), `formation_shape` (shape, spacing, tolerance, hold time).

## Checks and repair

| Level | What it finds |
| --- | --- |
| L1 static | unknown actions or parameters, fields read but never written, conditions true on entry, members issuing leader commands, vehicle counts, states without exits, wrong route names |
| L2 single tick | wrong transitions, missing event handling, cancel not reaching shutdown |
| L3 2D simulation | stuck states, members never firing, same-tick ping-pong, expired tasks, blocked line of fire, unmet obligations |

The repair loop (`falcon/v3/fixer.py`, tools in `falcon/v3/tools.py`) is a ReAct loop with
one JSON action per step: read tools (`check`, `show`, `explain_check`, `eval_condition`,
`who_writes`, `history`, `simulate`, ...), structured writes (`set_condition`, `set_trigger`,
`add_transition`, `use_tree_event`, `set_state_tree`, `set_expectation`, ...), `try_edit`,
`revert`, `ask_human`, `run_afsim`. Each step's prompt is rebuilt from state (current check,
last 5 steps, changes tried). Repeated changes, constant conditions and events used as
conditions are refused; deleting tests or scenarios is refused.

## AFSIM

`ir/v2/afsim/` compiles a package to AFSIM (advanced behavior trees), writes a scenario per
model (enemy distance and run time from the model and the requirement; values the user set are
kept), runs `mission`, judges the event output (contacts, distances, weapon hits and kills,
formation geometry) and builds an `.aer` replay. Branch scenarios cover a model's exception
branches after questions to the user.

## Small-model post-training

Training runs on a Linux GPU server; scoring runs where the checker and capability set are.
See `scripts/train/README.md`.

| Step | Module | Result |
| --- | --- | --- |
| Model generation | `training/pilot_fp.py`, `strike_fp.py`, `reform_fp.py` | 300 formation/perception models, all passing the checks, 38 structures; 10/10 AFSIM spot checks pass |
| Requirement back-translation | `training/backtranslate.py` | 1200 texts in 4 styles -> 954 kept by rule + LLM fact checks; 934 pairs |
| Sample rendering | `training/render.py` | structure / control / verification samples with the online prompts; split by model |
| Error injection | `training/inject.py` | 10 error types, 3925 injections, 5628 repair samples, each re-checked |
| SFT | `scripts/train/` | Qwen3-8B QLoRA (r=32, all linear layers, completion-only loss) |
| Real-sheet evaluation | `training/realeval.py` | repair loop on real failed sheets, optional AFSIM loop |
| RL reward | `training/reward.py` | GRPO trajectory reward (draft): outcome, potential-based progress, held-out scenario, bypass / invalid / no-edit penalties, faithfulness gate |

## Legacy: IR 1.0 and Falcon v1

`ir/` (IR 1.0, `behavior_spec.yaml`, co-simulation protocol `tbe-step/1`, C++ engine host
skeleton in `integrations/`) and `falcon/` v1 (`draft`, `confirm`, `status`) are the earlier
contract. They are kept and tested but new work uses Falcon v3 and IR 2.0.

## Tests

```bash
pytest -q                       # network-free unit tests
ruff check src tests
pytest -m live -s tests/live    # opt-in: real Tavily / OpenAlex / DashScope calls
```

Tests that need the capability set run on the machine that holds it.

## Documents

- `docs/design/`: IR 2.0, Falcon v3 obligations and checks, capability set semantics,
  orchestrator, engine protocol.
- `docs/handoff/`, `docs/analysis/`: progress notes and experiment results.
- `scripts/train/README.md`: training environment and pipeline.

`THIRD_PARTY_NOTICES.md` lists third-party components; `frontend/` is based on
deep-agents-ui (see `frontend/UPSTREAM.md`).
