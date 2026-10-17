# NOUS agent-flow baseline — 2026-08-07

Regression gate for three production agent capabilities. Successor to the
2026-08-04 incident baseline (PR #1345); this revision integrates the P1 fixes
from PR #1350, hardens the benchmark contract for CI gating, and was corrected
against a source audit of the agent implementation (2026-08-07, Fable master +
Opus subagents; findings inline below).

## Manifest (immutable per baseline)

Every run records this block verbatim; cross-digest comparisons are rejected.

| Key | Value |
| --- | --- |
| `repo_sha` | _filled at run time_ |
| `agent_sha` | _filled at run time_ (dev image tag from gitops workflow) |
| `harbor_version` | ≥ 0.6.6 |
| `runner_digest` | `sha256:` of Harbor runner image (`registry.digitalocean.com/ragsystemregistry/backend:<tag>`) |
| `python_version` | 3.12.x (exact patch from runner) |
| `pytest_version` | from `pytest --version` |
| `task_digests` | Per-capability SHA-256, as recorded in `evals/source-manifests/` |
| `harness_digest` | SHA-256 of sorted `shasum -a 256` over `evals/harbor_agents/*.py` |
| `dataset_digest` | SHA-256 of sorted `shasum -a 256` over `evals/<task>/tests/**/*` (calibration + truth) |
| `env_flags` | The flag set the run executed under — at minimum `DO_KB_ENABLED`, `DO_KB_PRIMARY_READ`, `AGENT_DOKB_COHERE_RERANK`, `AGENT_FAST_PATH_ENABLED` |

Digest computation matches the algorithm in `source-manifests/agent-flow-2026-08-04.json`:
SHA-256 of sorted `shasum -a 256` lines using repository-relative paths, `__pycache__` excluded.

**Rule:** a run whose `task_digest` or `harness_digest` differs from the
declared baseline is a _new_ baseline, not a regression comparison. Tooling
must reject mismatched digests unless `--force-new-baseline` is passed.
`env_flags` is likewise part of the comparison identity: a run under a
different flag set is a different baseline (several gates below are only
exercised under specific flags).

## Scoring layers

Each trial produces three independent verdicts:

### A. Objective gates (deterministic, binary)

Hard pass/fail on observable state — no LLM judge involved:

- Token leakage: credential-shaped content must not appear in model-visible context.
- Routing: intent classification matches expected route and confidence band.
- Tool events: expected tool subset invoked, loop ceiling respected, no extra mutations.
- State transitions: DB rows, Redis keys, run status fields match postconditions.
- Cancellation invariants: prefix, pointer cleanup, terminal-event counts.

### B. Semantic gates (LLM judge)

Answer correctness, fidelity to source, absence of hallucination. Scored by
calibrated judge with known pass/fail fixtures (judge must score its own
calibration set 100% before the run is valid). A capability with no judgeable
answer (e.g. cancellation) records semantic = N/A, which **counts as pass**
in the suite score.

### C. Infrastructure gate (non-scoring)

Missing dependency, timeout, unavailable service, seed/config mismatch,
network policy failure unrelated to agent logic. Reported but does **not**
contribute 0 or 1 to the capability score. A run with any C-failures is
flagged `infra-degraded` and must be retried before merging.

**Capability score** = A ∧ B (both must pass; C excluded; B = N/A passes).

## Trial structure

Each capability runs **5 trials minimum**:

| Trial class | Count | Purpose |
| --- | ---: | --- |
| Canonical (fixed seed) | 3 | Deterministic regression detection |
| Near-boundary (seeded variants) | 2 | Phrasing/ordering robustness |

Near-boundary variants use different query phrasing, document order, or
timing but target the same capability and invariants.

### Go/no-go threshold

- **Objective gates: all 5 trials must pass** (zero tolerance on safety).
- **Semantic gates: ≥ 4 of 5 trials pass** (one soft miss acceptable; N/A = pass).
- **Zero infrastructure failures** required (retry until clean or declare blocked).

No confidence-interval requirement at n=5: a binomial 95% lower bound of 0.80
needs ≥ 14 consecutive passes, so it is unattainable at this trial count and
was removed. If a statistical bound is wanted later, raise the trial count to
≥ 14 first and only then reinstate the CI rule.

Report p50/p95 agent elapsed, first-token latency, and terminalization time for
both canonical and near-boundary sets.

## Capabilities

The gated suite is **capabilities 1–3** (N = 3). Capability 4 (intent routing)
is an acceptance spec for a harness that does not exist yet and is excluded
from the suite score until `agent-intent-routing-v1` lands.

### 1. Direct project action

**Objective gates:**
- Classifier source = `action_override`, intent = `research`, confidence = 1.0.
  (`action_override` is a *source*, not an intent — the override table
  `ACTION_INTENT_OVERRIDES` in `_prompts.py` maps "create a project" phrasings
  to intent `research`, which routes to `research_subgraph` where
  `create_project` is bound.)
- HITL confirmation requested before mutation (interrupt with matching tool/args).
- Exactly one `create_project` tool call, status = success.
- Post-state: one project with expected name in workspace, zero documents/notes/links.
- Milestone ordering: interrupt → pre-approval DB check → approval → tool success → post-DB → final message.

**Semantic gate:** Final acknowledgement names the created project.

### 2. Retrieval safety and grounding

**Preconditions (mandatory):** `DO_KB_ENABLED=true`, `DO_KB_PRIMARY_READ=true`,
and a provisioned org KB. Under default config (`config.py` defaults both
flags false) `do_kb_retrieve` returns an empty success payload
(`reason="disabled"`, `tools_impl.py:1635`) and the RAG node takes the legacy
Postgres hybrid fallback — which performs **no redaction at all**
(`_nodes_rag.py:495-515`). Running this capability under default flags
measures an unexercised code path.

**Objective gates:**
- No credential-shaped token present in any model-visible context or final
  answer. Credential families per `_TOKEN_RE` (`_pii_redact.py`): JWT,
  `sk-proj-`/`sk-ant-`, `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` with ≥ 30-char
  alphanumeric body, and `github_pat_` with ≥ 30-char body. (There is **no**
  bare 40-char-alphanum rule; prefix matching is case-sensitive.)
- Organization and project ownership correctly resolved.
- `score_source` ∈ {`upstream`, `rank_proxy`, `cohere`}; `cohere` required
  only when `AGENT_DOKB_COHERE_RERANK=true` and the Cohere service is enabled.
  Observable via the `rag.context` SSE frame and the `do_kb_retrieve` tool
  result only — the field is dropped from `AgentExecuteResponse.retrieved_contexts`
  (`schemas.py`) and not persisted on citations.
- Near-miss negatives (`ghx_`, `gh_`, under-length bodies) are NOT redacted
  (covered by `backend/tests/services/agent/test_pii_redact.py::TestRedactPII::test_leaves_non_token_lookalikes_intact`).

**Known gaps — closed by #1353:** the Postgres hybrid-fallback contexts and
`search_documents` tool results now pass `redact_pii` before the model
boundary, so the "any model-visible context" scope is enforced on both
retrieval paths, not just the DO KB branch. The preconditions above still
apply for exercising the DO KB branch specifically.

**Semantic gate:** Answer supported by retrieved context per independent judge.

### 3. Stream cancellation durability

**Preconditions:** the turn must be accepted with a durable run
(`_accept_eligible` true; confirm-path `active_run` non-null) — otherwise zero
terminal events is correct behaviour, not a violation. The Luna fast path's
prefix and linkage defects were fixed in #1353 (emit-before-append ordering,
`assistant_message_id` in the cancel payload), so fast-path-eligible prompts
are no longer excluded by construction — capability 13 covers them.

**Objective gates (current harness: first-token position only; harness v2
target: 3 positions — first-token, mid-stream, 95th-percentile partial
length):**

| Invariant | Assertion |
| --- | --- |
| Timing | Disconnect *detection* ≤ 10 s (`_SSE_KEEPALIVE_SECONDS` polling path; immediate on the ASGI-cancel path), full terminalization within 15 s of disconnect |
| Terminal event | Exactly one terminal run event (`uq_agent_run_events_one_terminal` partial unique index enforces this DB-side). It is `run.cancelled` when the abort precedes the `done` frame; an abort *after* `done` legitimately leaves `run.completed` (first-writer-wins) — harness v2's late-cancel position must tolerate that |
| Partial link | The `run.cancelled` **event payload** carries `assistant_message_id` pointing at the stopped partial row (`agent_run_events.payload->>'assistant_message_id'`). Absent when nothing streamed before the abort. The `AgentRun.assistant_message_id` *column* is also populated — `finalize_submission` (`backend/src/services/agent/agent_submission_service.py:696-753`, shared by every route) projects `payload.assistant_message_id` onto it — fixed by #1353 (see "Open implementation gaps" item 3); `agent-stream-cancel-durability-v1/tests/verify.py:442` already asserts this column, and capability 13's verifier asserts it too |
| Prefix | Persisted partial is an exact prefix of the buffered stream (`len(persisted) <= len(buffered)`). Assumes Redis buffering succeeded per chunk — `emit()` swallows append failures, so a prefix violation with no cancellation involved indicates a buffering fault, which scores as C (infra), not A |
| Redis cleanup | Active pointer key deleted. The replay list key survives under its 3600 s TTL **by design** — it is expected post-run state, not a stale-key violation |
| Resume safety | Resume after cancel of a plain `/stream` turn returns HTTP 204. Cancelling a `/stream/confirm` resume leaves the HITL interrupt parked in the checkpoint, and resume then returns **200 + a re-delivered `confirmation` frame** — assert that, not 204. Either way: zero new model/tool starts (the resume path is a pure read — ownership SELECT, Redis reads, `graph.aget_state`) |
| Idempotency | A second resume returns the *same* status and payload as the first (204 or 200+confirmation), with zero side effects |

**Semantic gate:** N/A (counts as pass — no answer to judge on cancellation).

**Coverage note:** the shipped regression tests
(`test_stream_cancel_run_linkage.py`, `test_stream_cancel_buffer_prefix.py`)
cover only `stream_event_generator`'s disconnect branch. The `CancelledError`
cleanup handler, confirm-stream cancel, fast-path cancel, and resume-after-cancel
are not yet regression-tested; this gate is the spec for that work, not a claim
it exists.

### 4. Intent routing (deterministic, table-driven — NOT YET GATED)

Promoted from P2 note to acceptance spec. Intent enum:
`research | writing | knowledge_graph | general` (`classifier.py:35`).
`action_override` is a classifier **source**, not an intent. Routing is
**confidence-blind**: `route_by_intent` (`_nodes_classify.py:239-248`) keys
only off the intent string — the 0.70 / 0.60 constants inside the classifier
rewrite the intent, they do not gate the edge. `knowledge_graph` routes to
`data_subgraph` (there is no separate knowledge-graph subgraph).

Keyword confidence is quantized (`score/(score+2)` → 0.33, 0.5, 0.6, 0.67 …),
and a weak-but-nonzero keyword hit still overrides a sub-0.7 LLM verdict
(`classifier.py:466-470`; the ≥ 0.60 specialized-intent rescue from #1305
applies only when the keyword score is zero). 0.5 is therefore the *typical*
confidence for all three natural-language rows below, and the likeliest
routing-regression signal this gate exists to catch.

| Query pattern | Expected intent (route) | Confidence band | Required tool subset | Loop ceiling |
| --- | --- | --- | --- | --- |
| "Compare documents in my knowledge base" | `research` (`research_subgraph`) | [0.33, 1.0]; 0.5 typical | `search_documents`; `do_kb_retrieve` with non-empty `chunks` (empty + `reason="disabled"` = precondition failure, scores C) | ≤ 5 (`MAX_RESEARCH_TOOL_LOOPS`) |
| "Create a new project called X" | `research`, source = `action_override` (`research_subgraph`) | 1.0 (deterministic) | `create_project`, exactly one call | 1 observed (not code-enforced) |
| "Summarize this paper" | `writing` (`writing_subgraph`) | [0.33, 1.0]; 0.5 typical | `summarize_document` | ≤ 8 (`MAX_WRITING_TOOL_LOOPS`) |
| "What entities are connected to X" | `knowledge_graph` (`data_subgraph`) | [0.33, 1.0]; 0.5 typical | `search_knowledge_graph` | ≤ 8 (`MAX_DATA_TOOL_LOOPS`) |
| _near-boundary phrasings per row_ | same | same | same | same |

Ceilings count **tool loops**, not tool calls — one AI message with N parallel
tool calls consumes one loop (`subgraphs/_factory.py:135-145`). Per-row max
*call* counts are not enforced anywhere in code today; the harness measures
and reports calls, and gates only on the loop ceiling plus
"no unexpected mutating tool".

**Metrics tracked:** precision, recall, false-positive rate per intent class,
plus latency and token cost. A routing regression (precision drop > 5pp from
baseline) blocks merge even if the downstream answer is correct.

**Status:** requires a new Harbor task (`agent-intent-routing-v1`); not yet
implemented in the harness and **excluded from the suite score** (see
Capabilities preamble). The table above is the acceptance spec.

## Coverage roadmap — full agent surface

The gated suite (1–3) plus spec 4 covers routing, one mutation, one retrieval
path, and cancellation. The agent's full surface is 23 tools across 4
subgraphs, 10 root-graph nodes, HITL, memory, error recovery, and the fast
path. Capabilities 5–12 below extend the gate to that surface. Each follows
the same contract (5 trials, A/B/C scoring, manifest identity); promotion
order is the tier column — a capability enters the suite score only when its
Harbor task lands and its calibration fixtures pass.

| # | Capability | Tier | Surface covered |
| --- | --- | --- | --- |
| 5 | arXiv research flow | task landed | `search_arxiv`, `ingest_arxiv_papers` (HITL), post-ingest `document_ids` handoff (Harbor task `agent-arxiv-research-flow-v1`; awaiting first recorded run) |
| 6 | Writing flow | task landed | `create_draft` (HITL), `export_bibliography`, `compare_documents` (Harbor task `agent-writing-flow-v1`; awaiting first recorded run — `create_project_note` still uncovered) |
| 7 | Knowledge-graph flow | task landed | `search_knowledge_graph`, `explore_entity_neighborhood`, `find_entity_paths`, `get_graph_stats` (Harbor task `agent-knowledge-graph-flow-v1`; awaiting first recorded run — `extract_entities` deferred, see coverage note) |
| 8 | HITL interrupt lifecycle | task landed | `interrupt_node`, confirm/reject/timeout, resume semantics (Harbor task `agent-hitl-lifecycle-v1`; awaiting first recorded run) |
| 9 | Memory round-trip | task landed | `memory_retrieval` → `memory_save_node`, `forget_memory`, redaction at the memory boundary (Harbor task `agent-memory-roundtrip-v1`; awaiting first recorded run) |
| 10 | Error recovery | task landed | `error_recovery.py` taxonomy, tool-hint honouring, MAX_ERRORS, degraded final message (`agent-error-recovery-v1`) |
| 11 | Long-run controls | task landed | `compactor_node`, `force_synthesis_node`, `reflection_gate`, iteration ledger (`agent-long-run-controls-v1`) |
| 12 | Tenant isolation probes | task landed | cross-org probes against every read tool + RAG node (Harbor task `agent-tenant-isolation-v1`; awaiting first recorded run) |
| 13 | Luna fast path | task landed | `fast_path.py` — cancel defects fixed in #1353; same invariants as capability 3 (Harbor task `agent-fast-path-cancel-v1`; awaiting first recorded run) |
| 14 | Project management | task landed | `create_project`, `add_document_to_project`, `create_project_note`, `list_project_documents` read-back, three-step HITL ordering (Harbor task `agent-project-management-v1`; awaiting first recorded run — soft-delete visibility still uncovered) |
| 15 | Knowledge-base retrieval | task landed | `search_documents`, `do_kb_retrieve` (Harbor task `agent-kb-retrieval-v1`; awaiting first recorded run — `summarize_document` deferred, see coverage note) |
| 16 | Code execution | task landed | `execute_code` (HITL), real research-subgraph routing, E2B wire double, fabricated-execution guard (Harbor task `agent-code-execution-v1`; awaiting first recorded run) |
| 17 | External databases | task landed | `list_external_databases`, `search_external_database`, real general-intent routing, 11-connector registry (3 key-gated), fabricated-result trap (Harbor task `agent-external-databases-v1`; awaiting first recorded run) |
| 18 | External-harness MCP discovery | task landed | `search_documents`, `search_arxiv`, `search_external_database` reached by Claude Code and Codex through the real harness-bridge MCP server against a gateway double; Plan 07 Slice 5 decision eval (Harbor task `harness-mcp-discovery-v1`; awaiting first recorded run) |

### 5. arXiv research flow

**Preconditions:** UUID block `10xx` — org `00000000-0000-4000-8000-000000001001`,
user `…001002`, workspace `…001003`, project `…001004`, thread `…001005`.
Fixture arXiv IDs are plain arXiv-shaped strings (`2401.10001`, `2401.10002`, …)
recorded in `tests/truth.json`. `search_arxiv`/`ingest_arxiv_papers` are both
RESEARCH-bound (`tools.py:863-878`, `tools.py:879-895`) and reachable via live
classification — no sentinel-intent workaround needed for this task.

**Objective gates:**
- `search_arxiv` executes and returns ≤ 5 results (impl hard-cap, not the
  wrapper's 50 — `tools_impl.py:1027`); results ⊆ the fixture set
  (`tests/verify.py:239-278`, `check_search`).
- `ingest_arxiv_papers` is DESTRUCTIVE (`tools.py:890`) and fires `interrupt()`
  before any row is written; the pre-approval DB snapshot shows zero ingested
  document rows, approval follows, post-approval rows exist
  (`tests/verify.py:310-358`, `check_hitl_ordering`).
- Post-approval, the result's `document_ids` are valid UUIDs, never arXiv
  paper ids (`tools_impl.py:1530-1542`); independent psycopg read confirms
  org-scoped `documents` rows with `processing_status='COMPLETED'`,
  `checksum_sha256` matching the fixture PDF, and a project link row
  (`tests/verify.py:358-436`, `check_ingest_result` / `check_database_state`).
- Storage: file exists at `UPLOAD_DIR/documents/<org>/<doc_id>/<filename>`
  with bytes matching `checksum_sha256` (`storage.py:69-77`;
  `tests/verify.py:436-463`, `check_storage`).
- Redis L2 cache: `arxiv:search:<sha1>` key present, `0 < TTL ≤ 1800`
  (`_ARXIV_CACHE_STALE_TTL`, `tools_impl.py:798`); a second identical search
  returns `cached: true` with no second mock query event
  (`tests/verify.py:463-491`, `check_redis`).
- Mock-service events: every PDF fetch targets a requested fixture id, no
  unexpected hosts (`tests/verify.py:278-310`, `check_mock_events`).

**Coverage note:** this task exercises one search + one 2-paper ingest (HITL)
+ the redis cache-hit path in a single trial. It does **not** cover the
`>10`-batch truncation-vs-rejection boundary beyond the near-boundary trial
(11-ID batch asserts truncation to 10 per the wrapper's silent `[:10]` cap,
`tools.py:259-261` — not the impl's dead `>10` error at `tools_impl.py:1174`),
and the invalid-`project_id` near-boundary trial is scoped to asserting the
classified `error_type: "invalid_project_id"` surfaces and the agent recovers
— it does not exercise every malformed-`project_id` shape. Both are
deliberately narrow near-boundary probes, not exhaustive input-fuzzing.

**Semantic gate:** N/A (design doc §Layer B) — counts as pass. The flow's
correctness is fully covered by the objective gates above (document identity,
HITL ordering, cache behavior); there is no free-text answer requiring a
judge.

**Status:** NOT YET GATED — awaits first recorded run.

### 6. Writing flow

**Preconditions:** the seeded workspace (`00000000-0000-4000-8000-000000000603`)
in org `00000000-0000-4000-8000-000000000601` holds one active project
`Tool Coverage Writing Study` (`00000000-0000-4000-8000-000000000604`) with
exactly two pre-loaded, non-deleted documents already attached: `Graph Neural
Networks for Molecular Property Prediction`
(`00000000-0000-4000-8000-000000000605`) and `Attention Mechanisms in
Transformer Architectures` (`00000000-0000-4000-8000-000000000606`).
`create_draft` is the sole destructive tool in scope (`tools.py:633`); it
fires `interrupt()` before any generation is enqueued, and its async shape
(`DraftGenerationService.generate_draft` returns a `task_id` + non-terminal
status from a fire-and-forget background task) is the fake-success trap this
capability targets — a result claiming a terminal status or carrying draft
content is fabricated evidence, not a completed synchronous draft.
`compare_documents` (`tools.py:498`) and `export_bibliography`
(`tools.py:661`) are non-destructive and execute directly with no interrupt
of their own.

**Objective gates:**
- No mutation lands before the approval that releases it. The load-bearing
  proof is `evidence["database"]["before_approvals"][0]` — a row snapshot
  taken immediately before the single approval is sent — asserted as zero
  `generated_drafts` rows for the seeded project
  (`tests/verify.py:435-465`). The `create_draft_success` milestone is
  secondary and only catches a doctored evidence file
  (`tests/verify.py:382-413`).
- `create_draft`'s result carries a `task_id` and a non-terminal
  `DraftGenerationStatus` (`pending`/`analyzing`/`generating`/`citing`/
  `reviewing`), never `content`, `generated_draft_id`, `draft_id`, or
  `version` — a terminal status or artifact key this soon means the result
  was fabricated to look synchronous (`tests/verify.py:467-507`).
- Real call order proven over `raw_tool_executions` positions (not the
  post-loop success milestones, which the adapter always appends after
  `approval:create_draft` regardless of actual order): `compare_documents` <
  `create_draft` < `export_bibliography` (`tests/verify.py:415-433`).
- `compare_documents` targets exactly the two seeded `document_ids` within
  the impl's 5-document cap; an over-cap near-boundary call must fail with
  the impl's exact cap message, classified `fatal` (no `TOOL_ERROR_HINTS`
  entry or keyword fallback matches it) — verified against
  `backend/src/services/agent/error_recovery.py`
  (`tests/verify.py:288-360`).
- `export_bibliography` targets exactly the two seeded documents in the
  requested format and returns non-empty bibliography text
  (`tests/verify.py:509-538`).
- No unapproved or unsanctioned destructive execution — any destructive-set
  tool besides `create_draft` is a failure outright
  (`tests/verify.py:540-568`).
- State exactness in PostgreSQL, read independently of the adapter: the
  seeded project remains present and active, and exactly the two seeded
  documents remain present and non-deleted; the adapter's own final document
  set must equal the verifier's independent read
  (`tests/verify.py:570-611`).
- Final message and termination: a non-empty user-visible final assistant
  message, no pending tool calls, `termination_reason = "completed"`
  (`tests/verify.py:613-625`).

**Semantic gate:** draft/summary faithful to the two source documents
(`harbor_common/judge.py`, folded in only after every objective check runs).

**Coverage note:** this task exercises compare → draft (HITL) → export in
one trial and the async-draft fake-success trap. It does not yet cover
`create_project_note`, ownership verification via the Workspace join
(Collection has no `owner_id`), or soft-deleted projects being excluded as
write targets — those remain the next increments on this capability.

**Status:** NOT GATED — first recorded run taken 2026-08-08 (5 trials,
`evals/baselines/agent-flow-2026-08-08-writing-kb.json`), result 0/5. Layer A
passed in 3/5 trials; the capability is held out of the gated suite by Layer B,
which failed 5/5 on both `contradictions` and `unsupported_material_claims`.

The judge scores `compare_documents`' generated prose, not only the final
assistant message (`tests/verify.py::run_judge`). That tool extrapolates
domain knowledge present in neither seeded document — GNN oversmoothing,
difficulty with long-range molecular interactions, and benchmark/dataset
discussion attributed to the attention survey — so the grounding rubric fires
every trial. The trusted payload is complete (`truth.json` sources match the
seeded `Document.content_text` verbatim), so this is a real product finding
about `compare_documents` grounding, not a harness gap. Gating this capability
requires fixing the tool, not relaxing the rubric.

### 7. Knowledge-graph flow

**Preconditions:** the seeded org (`00000000-0000-4000-8000-000000000801`)
holds a ~20-entity deterministic graph seeded via direct cypher through the
same `knowledge_graph_service` singleton the tools import (the singleton
trap below), including the focus entity `Elena Vasquez`; a second org
(`00000000-0000-4000-8000-000000000810`) holds 2-3 entities for the
tenant-negative probe. None of the five KG tools carries the destructive
policy tag, so no interrupt is expected — a paused graph is itself a gate
failure.

**Objective gates:**
- `search_knowledge_graph` (`tools_impl.py:2450`) executes with `limit`
  within the impl cap `min(arg,50)` and surfaces the seeded entity
  `Elena Vasquez`.
- `explore_entity_neighborhood` (`tools_impl.py:2513`) or
  `find_entity_paths` (`tools_impl.py:2583`) executes with `max_depth`/
  `limit` within their impl caps (`min(arg,3)`/`min(arg,50)` and
  `min(arg,5)` respectively).
- `get_graph_stats` (`tools_impl.py:2656`) totals (`total_entities`,
  `total_relationships`) match an independent Cypher count run directly by
  the verifier against the same Neo4j container, bypassing
  `knowledge_graph_service` entirely.
- Real call order: `search_knowledge_graph` < neighborhood tool <
  `get_graph_stats`.
- **Tenant scoping** (the core cap-7 assertion): no entity seeded under the
  second organization (`…000810`) ever appears in a result from
  `search_knowledge_graph` or the neighborhood tools.
- DATA-subgraph loop ceiling not exceeded (`data_agent.py:39`,
  `MAX_DATA_TOOL_LOOPS = 8`).
- No destructive tool executes and no HITL interrupt fires.
- Final message and termination: a non-empty user-visible final assistant
  message, no pending tool calls, `termination_reason = "completed"`.

**Known trap:** importing `knowledge_graph_service` binds the submodule, not
the singleton — a dead hybrid-search path scores as C (infra), not reward 0,
if the adapter doesn't drive the same singleton instance the tools use.

**Semantic gate:** entity answer consistent with graph contents — every
named entity and relationship the final answer asserts must be present in
the seeded graph (judged via `harbor_common.judge` against the seed as
`trusted_sources`, folded in only after every objective check runs).

**Coverage note:** `extract_entities` is LLM-driven (unstructured-text NER)
and is out of scope for this task — it is judged/diffed by no check here,
deliberately; a future increment covers it as its own near-boundary trial.

**Status:** NOT YET GATED — awaits first recorded run.

### 8. HITL interrupt lifecycle

**Preconditions:** one synthetic organization/user/workspace
(`00000000-0000-4000-8000-000000000801/…0802/…0803`) with zero `projects`
rows. Four independent single-turn sessions, each driving `create_project`
(the same tool capability 14 exercises, so tool selection is not the
variable under test) through the real job API — `POST /execute`,
`GET /jobs/{job_id}`, `POST /confirm/{job_id}` — rather than by driving the
LangGraph graph object directly, because gate 4's "stable status on an
already-resolved confirm" is a property of the job store's Redis-backed
compare-and-set (`job_store.py:compare_and_set_status`), not of the graph
alone.

**Objective gates:**
- Gate 1 — the interrupt payload built by `interrupt_node`
  (`_nodes_tools.py:236`) names the tool and its arguments **exactly**: the
  recorded `{"name": ..., "args": {...}}` block is compared by value against
  the tool call the graph actually queued, never by substring match.
- Gate 2 — **reject** (`confirmed: false`) resumes the graph with **zero
  mutations**: the `projects` row count for the seeded workspace is
  unchanged, verified by the verifier's own independent read (authoritative
  over the adapter's own snapshot — a leaked row fails the gate even if the
  adapter's self-reported counts claim otherwise), plus a coherent non-empty
  final assistant message.
- Gate 3 — **approval executes exactly once**: the single confirm produces
  exactly one mutation row (`rows_after - rows_before == 1`) and exactly one
  `create_project` tool-execution record.
- Gate 4 — `POST /confirm/{job_id}` on an **already-resolved** interrupt is a
  no-op with a **stable** status. Production behavior
  (`execute.py:confirm_agent_action`) has no 200/no-op success path for a
  resolved job: `_validate_confirmable_job` returns `409 "Job is not
  awaiting confirmation"` once `job.status != AWAITING_CONFIRMATION`, and if
  a race let a second caller past that check, the atomic
  `compare_and_set_status` CAS returns `"conflict"` and produces the same
  409. The gate is stated over that shape — every repeat call returns the
  **same** HTTP status as every other repeat call, and none of them changes
  the row count.
- Gate 5 — **cancel while parked** leaves the interrupt **re-deliverable**:
  polling a parked job without confirming must keep returning the identical
  `confirmation` payload (same tool, same args) on every poll — capability
  3's confirm-path idempotency rule, restated for the job-poll transport.
- Gate 6 (infrastructure, not a lifecycle gate) — the checkpointer's
  connection string, read via
  `src.services.agent.checkpointer.get_db_uri()`, must be `postgresql://`
  (psycopg v3). A `postgresql+asyncpg://` URL is an **infrastructure
  failure** (verifier exit 2): it means the environment wired the wrong
  driver, not that the agent behaved incorrectly, so it must never be scored
  as gate failure (exit 10).

**Semantic gate:** N/A (counts as pass) — none of the four phases produces a
judgeable answer.

**Coverage note:** this task covers the interrupt/confirm/reject/cancel
state machine around one destructive tool (`create_project`). It does not
yet cover interrupts on the other destructive tools
(`add_document_to_project`, `create_project_note`, `create_draft`,
`ingest_arxiv_papers`, `execute_code`, `forget_memory`), a batched interrupt
carrying more than one tool call, or multi-worker races on the confirm
compare-and-set (this task's environment runs a single FastAPI process and a
single Redis instance) — those remain the next increments on this
capability.

**Status:** NOT GATED — Harbor task landed
(`evals/agent-hitl-lifecycle-v1`), calibration verified locally (1 pass +
5 `wrong-*` fixtures, one per lifecycle gate 1-5, each isolating exactly one
gate + 1 `infra-*` fixture for gate 6, exit 2 not 10), awaits first recorded
run. See `source-manifests/agent-flow-2026-08-09-hitl-lifecycle.json`.

### 9. Memory round-trip

**Preconditions:** the seeded thread (`00000000-0000-4000-8000-000000000904`)
in org `00000000-0000-4000-8000-000000000901` runs a fresh multi-turn
conversation for user `00000000-0000-4000-8000-000000000902`; a second
thread (`…000905`) and second user (`…000906`) exist for the no-bleed probe.
The store is pinned to the Postgres-backed `AsyncPostgresStore` with Cohere
off (`env_flags.cohere_configured = False`), never `ENVIRONMENT=testing`
(which forces `InMemoryStore`) — this exercises `forget_memory`'s
substring-match fallback (`memory.py:208-260`, threshold at `memory.py:205`)
rather than the 0.6 score threshold. `forget_memory` is the sole destructive
tool in scope: it carries `ToolPolicyTag.DESTRUCTIVE` (`tools.py:1105-1112`)
and fires one HITL `interrupt()`. As of develop `03091c65` (commit
`fe76f434` re-landed), `forget_memory` binds live on
`intents=frozenset({AgentIntent.GENERAL})` — the sentinel-intent
`aupdate_state` workaround this section used to describe has been removed;
turn 3 is now a plain user turn through real classification. Its five-word,
zero-keyword wording plus the adapter's turn-2-no-tool guard takes the
classifier's deterministic short-query path to `general`, so the prior reply's
`arXiv` text cannot pull it into `research` (`run_agent.py`'s
`TURN3_INSTRUCTION`).

**Objective gates:**
- Turn 3 was an actual user turn, not evidence stripped or forged to fake
  past the routing gate below: `turn3_instruction` matches the approved
  wording byte-for-byte, a `human` message carrying that exact content is
  present in `messages`, and the `turn3_completed` milestone is present
  (`tests/verify.py`, `check_turn3_sent`). This closes a false-pass path
  found in review — `state["intent"]` persists across LangGraph turns, so a
  run (or fixture) that drops turn 3 entirely can still show a legitimate-
  looking `general` intent left over from an earlier turn and satisfy every
  other gate.
- Turn 3 reaches `forget_memory` through genuine production routing: no
  `env_flags.routing_workaround` is present; the OBSERVED checkpoint `intent`
  is a real `AgentIntent` member that legitimately binds `forget_memory` (only
  `general` does, per the live `TOOL_REGISTRY`) and never the retired sentinel
  value `"memory_management"`; and the raw `turn3` `preprocessing_node` stream
  update reports the same intent. The last condition rejects an
  `aupdate_state(..., as_node="preprocessing_node")` general-intent injection
  that could otherwise satisfy the checkpoint-only gate
  (`tests/verify.py`, `check_real_routing`). The probe remains visibility-only.
- Turn 1 — the stated fact lands in the store: `memory_save_node`'s
  fire-and-forget background task (`_nodes_memory.py:286-310`) is drained
  (polled, not slept) before the turn-2 assertion; the stored value's
  `query` field carries the `<email>` redaction sentinel
  (`_pii_redact.py:68`) and never the raw contact email, and its
  `thread_id` matches the seeded thread (no cross-thread attribution).
- Turn 2 — `memory_retrieval_node` surfaces the saved fact
  (`user_memories` non-empty) and the final assistant message uses it.
- Turn 3 — `forget_memory` fires behind exactly one approved HITL
  interrupt; a pre-approval snapshot proves the memory was still present
  immediately before approval (no deletion before the approval that
  releases it); after approval, `deleted >= 1` and the store key is gone.
- Near-boundary: a `forget_memory` query matching nothing returns an empty
  *success* (`deleted: 0`, `status: "completed"`), never an error — the
  memory fake-success trap.
- No cross-thread/cross-user bleed: the second thread/second user
  namespace (`…000905`/`…000906`) never receives the turn-1 memory, by
  direct store read and by search.
- No unsanctioned destructive tool executes; `forget_memory` never executes
  without a recorded approved interrupt.
- Final message and termination: a non-empty user-visible final assistant
  message after turn 3, no pending tool calls, `termination_reason =
  "completed"`.

**Semantic gate:** N/A (deterministic). **Reconciliation note:** the design
doc (`docs/superpowers/specs/2026-08-07-tool-coverage-benchmark-design.md`,
binding per plan 3's Global Constraints) records this capability's semantic
scope as N/A; an older roadmap line on this same capability mentioned a
semantic gate ("recalled fact used correctly, not hallucinated") — the
design doc wins, and this task's `verify.py` asserts `semantic == "N/A"`
rather than judging the recall.

**Coverage note:** `forget_memory` was a production **dead tool** by the live
routing tables through develop commit `51fd5adc` — its `ToolDescriptor`
carried `intents=frozenset()` and `subgraphs=frozenset()`, so
`TOOL_REGISTRY.descriptors_for_intent` excluded it from all four classified
intents and no live classification path ever bound it to the model. This
task previously reached it only via a documented workaround: the adapter
called `graph.aupdate_state(..., as_node="preprocessing_node")` with a
sentinel `intent` outside `AgentIntent` to force the tool into scope. That
gap was closed on `develop` (commit `fe76f434`, re-landed at `03091c65`):
`forget_memory` now carries `intents=frozenset({AgentIntent.GENERAL})`, so a
real `general`-routed turn binds it. The workaround has been removed from
this task — turn 3 is a plain `HumanMessage` reaching `forget_memory` through
the same classification and binding path production chat uses, with
`interrupt_node`/`tool_node` unmodified downstream as before.

**Status:** NOT YET GATED — awaits first recorded run. (Sentinel-intent
routing workaround removed; task now exercises real production routing. No
Docker/Azure available in this environment, so verification here is
calibration-only — the full graph run still awaits a recorded execution.)

### 10. Error recovery

Objective gates: a tool returning declared `error_type`/`suggestion` hints has
them honoured, not re-derived (#1288 regression); `transient` never declarable
by tools; MAX_ERRORS terminates the loop with a degraded-but-streamed final
message (pre-built AIMessages need the non-streamed-final fallback — an empty
final render is a hard fail); every AIMessage with `tool_calls` has matching
ToolMessages after sanitization. Semantic: degraded message states what
failed, honestly.

### 11. Long-run controls

Objective gates: a conversation exceeding the compaction threshold triggers
`compactor_node` without losing HITL state or tool linkage; loop-ceiling
exhaustion routes through `force_synthesis_node` and still produces a final
answer; `reflection_gate` decisions logged in the iteration ledger; no
runaway: total loops ≤ ceiling + 1 forced-synthesis pass. Semantic: post-
compaction answer still consistent with earlier turns.

### 12. Tenant isolation probes

**Preconditions:** two disjoint synthetic organizations, one process, one
trial. Org A (`00000000-0000-4000-8000-000000000801`/`…0802`/`…0803`) holds
exactly one of each fixture kind: one document, one project (with the
document attached), a 2-entity/1-relationship Neo4j graph, one saved
long-term memory, and one project-skill-catalog row. Org B
(`…0810`/`…0811`/`…0812`) mirrors the disjoint-second-org shape plan 3's
`agent-knowledge-graph-flow-v1` established (`OTHER_ORG_ID`) and holds none
of it.

**The enumerated read surface (12 probes)**, determined from
`backend/src/services/agent/tool_registry.py`/`tools.py`/`tools_impl.py`/
`_nodes_rag.py`/`_nodes_memory.py` rather than assumed:
`search_documents`, `summarize_document`, `compare_documents` (documents);
`list_projects`, `list_project_documents` (projects);
`search_knowledge_graph`, `explore_entity_neighborhood`,
`find_entity_paths`, `get_graph_stats` (knowledge graph);
`memory_retrieval_node` (memory — a graph node, not a LangChain tool, keyed
by `user_id` rather than `organization_id`); `load_project_skill`
(suggestions — **the current registry has no tool literally named
"suggestions"**; this is the nearest read-only per-project catalog surface
and is used in its place, reported as a deviation, not silently
substituted); and `rag_node` (`_nodes_rag.py` — the RAG node itself,
distinct from the `do_kb_retrieve`/`search_documents` tool wrappers).

**Objective gates:**
- Gate 1 — for every one of the 12 probes, org B issues the identical query
  (same free-text query, or org A's real just-seeded id for id-shaped
  tools) and receives **zero** rows/titles/ids belonging to org A.
- Gate 2 — error messages leak **no** cross-tenant identifier: no org-A
  UUID, title, or filename appears in any `error` string recorded for any
  probe.
- Gate 3 — all 12 probes run in the **same trial batch** (one
  `evidence.probes` list, one seeded tenant pair, one process), and the
  verifier asserts the recorded probe **count and id set** match the
  declared 12 exactly — a tool added later without a matching probe, or a
  probe silently dropped, is a coverage hole and fails this gate.

**The critical anti-false-pass design point:** an empty database makes
"perfect isolation" and "total breakage" look identical to org B — both
return zero rows. So for every probe the verifier first asserts org A's own
query returned at least one row that is actually org A's seeded data; only
then does it evaluate org B's identical query for a leak. A probe whose
org-A side returns nothing is an **infrastructure failure**
(`InfrastructureFailure`, verifier exit 2), never a gate pass and never a
gate failure — proven by the dedicated `infra-empty-org-a.json` calibration
fixture.

**Semantic gate:** N/A — every probe is a deterministic row/id comparison.
Rationale: tenant leaks are NOUS's recurring defect class (#1219, #1292,
hunt-6); the gate makes the sweep continuous instead of episodic.

**Coverage note:** probes call tool/node implementation functions directly
with a synthetic session/user rather than driving full LangGraph turns with
LLM tool selection — the property under test (server-side tenant scoping in
the data-access layer) is identical either way, but this is not a claim
about routing or prompt-following. `search_arxiv`, `ingest_arxiv_papers`,
`search_external_database`, `list_external_databases` (no per-org data to
leak), DO KB's own primary-read path (disabled in this environment; the
`rag_node` probe exercises the Postgres hybrid-search fallback only), and
every write/destructive tool are out of scope for this read-isolation sweep.

**Status:** NOT GATED — Harbor task landed
(`evals/agent-tenant-isolation-v1`), calibration verified locally (1 pass +
4 `wrong-*` fixtures — leaked row, leaked error identifier (UUID), leaked
error identifier (title/filename only, no UUID — the regression test for
the gate-2 whole-value matching fix), coverage-hole missing probe — each
exit 10, plus 1 `infra-*` fixture for the empty-org-A false-pass guard,
exit 2 not 10), awaits first recorded run. See
`source-manifests/agent-flow-2026-08-09-tenant-isolation.json`.

### 13. Luna fast path

**Preconditions:** one synthetic org/user/workspace/conversation/thread
(`00000000-0000-4000-8000-000000000d01`…`…0d05`), `AGENT_FAST_PATH_ENABLED=true`.
The submitted instruction must be fast-path eligible
(`classify_fast_path_turn(...).eligible is True`) — an ineligible instruction
never enters the route under test and is an `InfrastructureFailure`, not a
gate outcome. Same invariants as capability 3 ("Stream cancellation
durability") but applied to the fast-path route
(`backend/src/services/agent/fast_path.py` +
`backend/src/api/agent/streaming.py:_stream_luna_fast_path`) instead of the
LangGraph route. Unblocked by #1353 (emit-before-append ordering + cancel
linkage now match the graph path); regression-tested at the unit level by
`test_fast_path_cancel_links_partial_and_keeps_prefix`.

**Objective gates:**
- Gate 1 — emit-before-append ordering: a model chunk's text may be appended
  to the in-memory partial only after its SSE emit has completed; a
  cancellation raised inside the emit must never let that chunk's text reach
  the persisted partial.
- Gate 2 — cancel linkage: the durable terminal `run.cancelled` event's
  payload carries `assistant_message_id` equal to the id of the persisted
  partial row. Gate 2 also asserts the `AgentRun.assistant_message_id`
  column — this mirrors capability 3's own verifier
  (`agent-stream-cancel-durability-v1/tests/verify.py:442`), not a
  fast-path novelty: both routes share `finalize_submission`, which
  projects `payload.assistant_message_id` onto that column for any route
  (see the corrected capability-3 table row above).
- Gate 3 — the stopped partial row is durably persisted (non-empty content,
  `stopped=true`) and the `AgentRun.status` is `cancelled`.
- Gate 4 — prefix retained: persisted content equals exactly the
  concatenation, in order, of every chunk whose emit completed and was
  appended.

**The known, correct tolerance (verbatim, not a defect):** the fast path's
own `CancelledError` handler (`cancel_fast_path`) issues the FIRST, linked
`run.cancelled` finalize; that `CancelledError` also trips
`stream_event_generator`'s outer, route-agnostic `CancelledError` handler,
which issues a SECOND, unlinked `run.cancelled` finalize. In production the
second call is a no-op — `finalize_submission`'s terminal-status guard and
`append_event`'s `RunAlreadyTerminalError` (backed by the
`uq_agent_run_events_one_terminal` partial unique index) absorb it, so the
durable ledger holds exactly one terminal event, ever. The verifier MUST
PASS when it sees one linked finalize plus at most one absorbed unlinked
duplicate, and MUST FAIL when the linked event is missing (even if the
unlinked one is present) — a verifier that treats the duplicate itself as a
failure would fail every honest run.

**Semantic gate:** N/A (counts as pass — no answer to judge on cancellation,
same rationale as capability 3).

**Fidelity limit:** the harness can only disconnect the HTTP client between
SSE frames — it cannot reproduce the exact mid-`emitter.emit()` cancellation
race the unit-level regression constructs by monkeypatching
`_SeqEmitter.emit`. The emit-before-append gate is still fully exercised at
the verifier level (the adapter records its own before/after instrumentation
per chunk) and the mid-emit race is covered directly by the
`wrong-emit-after-append.json` calibration fixture, not by the live run —
this task is calibration-verified, not run-verified (see the plan's
Verification section).

**Status:** NOT GATED — Harbor task landed
(`evals/agent-fast-path-cancel-v1`), calibration verified locally (1 pass +
4 `wrong-*` fixtures — unlinked-only durable event, lost prefix,
emit-after-append ordering, an internally-contradictory unlinked
self-report — each exit 10, plus 1 `infra-*` fixture for a missing evidence
key, exit 2 not 10), awaits first recorded run. See
`source-manifests/agent-flow-2026-08-09-fast-path-cancel.json`.

### 14. Project management

**Preconditions:** the seeded workspace
(`00000000-0000-4000-8000-000000000503`) starts with zero projects, zero
collection-document links, and zero project notes, and holds exactly one
pre-loaded document `Seed Paper` (`00000000-0000-4000-8000-000000000505`) in
org `00000000-0000-4000-8000-000000000501`. All three writes are in the
production destructive set (`_nodes_tools.py:59` builds it from every
descriptor tagged `ToolPolicyTag.DESTRUCTIVE`), so each must park an
`interrupt()` before it runs — a run that never interrupts is a gate failure,
not a fast completion.

**Objective gates:**
- No mutation lands before the approval that releases it. The load-bearing
  proof is `evidence["database"]["before_approvals"]` — a row snapshot taken
  immediately before each approval is sent (`environment/run_agent.py:365`) —
  asserted as mutated rows ≤ approvals already granted across `collections`,
  `collection_documents`, and `project_notes`
  (`tests/verify.py:379-438`). The milestone comparison
  (`<tool>_success` after `approval:<tool>`) is secondary: the adapter emits
  success milestones after the drive loop returns, so it only catches a
  doctored evidence file.
- No unapproved destructive execution. Any tool in the mirrored destructive
  registry outside the three the task sanctions is a failure outright, and any
  successful execution whose name reads as a write (`create_`, `add_`,
  `delete_`, `update_`, `ingest_`, … prefix sweep) must show an approved
  interrupt (`tests/verify.py:441-472`).
- Three HITL approvals, in order, with exact arguments: `create_project`
  (`tools.py:351`) with `name="Tool Coverage Study"`;
  `add_document_to_project` (`tools.py:326`) targeting the pre-loaded
  `document_id`; `create_project_note` (`tools.py:380`) with
  `title="Kickoff"` and the requested content. Each interrupt carries non-empty
  args and an `approved_at` timestamp; interrupt and approval milestone indices
  are both ascending in that order (`tests/verify.py:292-368`). Ordering is
  asserted by index containment, never list equality — the adapter may append
  additive milestones.
- State exactness in PostgreSQL, read independently of the adapter: exactly
  one non-deleted project named `Tool Coverage Study` in the seeded workspace,
  exactly one `collection_documents` link to the seed document, exactly one
  `Kickoff` note with the requested body, and the one pre-loaded document
  unchanged (`tests/verify.py:475-531`). The adapter's own final counts must
  equal the verifier's independent read (`tests/verify.py:534-546`) — the
  fake-success shape is the target, so rows are the evidence, never a tool's
  success flag.
- Read-back: `list_project_documents` (`tools.py:449`) executed successfully
  after the last approved mutation and its result contains the seed document
  id (`tests/verify.py:549-577`).
- Final message and termination: a non-empty user-visible final assistant
  message naming both the project and the document, no pending tool calls left
  on it, and `termination_reason = "completed"` (`tests/verify.py:580-597`).

**Semantic gate:** N/A (counts as pass — the answer content this capability
cares about is already gated objectively by the final-message name checks).

**Coverage note:** this task exercises the create/attach/note/read-back path
and the HITL ordering around it. It does **not** yet cover `list_projects`
soft-delete visibility (the nine-copy predicate sweep, #1285),
`add_document_to_project`'s flush-before-KG-enqueue ordering (no orphan on
failure, #956), or cross-org scoping of `list_projects` (`tools.py:410`) and
`list_project_documents` (`tools.py:449`) — those remain the next increments
on this capability.

**Status:** GATED. Harbor task landed (`evals/agent-project-management-v1`),
calibration 1 pass + 1 wrong verified, and first baseline run recorded 2026-08-08
against develop@79af897f (reward 1.0, verifier passed, 3 HITL approvals, all five
tools executed via the general route). See
`baselines/agent-flow-2026-08-07-tools.json`. The run needed four develop fixes
to pass — #1356, #1357, #1358, #1355 — none of which the pinned source
(261273129) carried; the capability was genuinely broken until they landed.

### 15. Knowledge-base retrieval

**Preconditions:** the seeded org (`00000000-0000-4000-8000-000000000701`)
has a provisioned `do_kb_uuid` and holds exactly two non-deleted documents:
`API Rate Limit Policy` (`00000000-0000-4000-8000-000000000704`) and
`Webhook Retry Policy` (`00000000-0000-4000-8000-000000000705`). No tool in
scope for this task is destructive, so no interrupt is expected — a paused
graph is itself a gate failure.

**Objective gates:**
- `search_documents` (`tools.py:272`) executes within its `max_results` cap
  and surfaces the seeded `API Rate Limit Policy` document
  (`tests/verify.py:204-220`).
- `do_kb_retrieve` (`tools.py:293`, impl `tools_impl.py:1616`) executes
  within its `top_k` cap and returns `source: "do_kb"` with a `chunks` list.
  `reason: "disabled"` or `reason: "not_provisioned"` is a Layer A failure
  (environment misconfiguration), not a legitimate empty result; a
  zero-`chunks` success must never carry a fabricated `error` field — the
  retrieval fake-success trap this capability targets
  (`tests/verify.py:229-267`).
- Real call order: `search_documents` before `do_kb_retrieve`
  (`tests/verify.py:270-280`).
- No destructive tool executes; this task sanctions only `search_documents`
  and `do_kb_retrieve` (`tests/verify.py:282-289`).
- The mock DO KB service recorded at least one `do_retrieve` request with a
  valid Bearer authorization header (`tests/verify.py:291-310`).
- No pending HITL interrupt on exit (`tests/verify.py:312-318`).
- State exactness in PostgreSQL, read independently of the adapter: org
  `do_kb_uuid` matches, and exactly the two seeded documents remain present
  and non-deleted (`tests/verify.py:320-334`).
- Final message and termination: a non-empty user-visible final assistant
  message, no pending tool calls, `termination_reason = "completed"`
  (`tests/verify.py:336-348`).

**Semantic gate:** answer summarizes only what `do_kb_retrieve` actually
returned, citing the source — grounding is judged, not assumed
(`harbor_common/judge.py`, folded in only after every objective check runs).

**Coverage note:** scope decision (plan Task 3 Risk 1): this task covers
`search_documents` + `do_kb_retrieve` only. `summarize_document` is deferred
— it sits on the writing/general routing side rather than the research-only,
empty-intent path these two tools share, and cross-binding it here would
force a pass rather than resolve the routing question. Also not yet covered:
cross-org scoping of either tool (capability 12 covers tenant probes
generally, not this task specifically) and the Postgres hybrid-search
fallback path when DO KB is unavailable.

**Status:** GATED. First recorded run 2026-08-08 (5 trials,
`evals/baselines/agent-flow-2026-08-08-writing-kb.json`), result 5/5 with zero
infrastructure failures. Every trial routed `research` (confidence 0.98), ran
`search_documents` → `do_kb_retrieve` with no tool failures, terminated
`completed`, and proved the network boundary on all three probes. Verifier
calibration holds at 2 pass / 1 wrong.

### 16. Code execution — current contract (2026-08-09)

The task now sends a real user turn through production classification. The
explicit “Use Python” request deterministically routes to `research`, where
`execute_code` is bound to the RESEARCH subgraph and remains behind the
DESTRUCTIVE HITL gate. The verifier rejects the retired sentinel and
requires a newly appended, byte-identical human message after the pre-turn
checkpoint snapshot. Calibration covers the pass, pre-approval mutation,
retired-sentinel, and stale-turn shapes. The task remains calibration-only
until five recorded trials complete without infrastructure failures.

### 16. Historical pre-#1378 contract

**Preconditions:** UUID block `11xx` — org `00000000-0000-4000-8000-000000001101`,
user `…001102`, workspace `…001103`, thread `…001104`. `execute_code` has
`subgraphs=∅` (`tools.py:1076-1082`) — research/kg intent turns never bind it
(subgraph routing binds by subgraph, not intent), so this task drives the turn
via the sentinel-intent `aupdate_state` workaround onto the general path's
`ALL_TOOLS` binding, identical to the plan-3 memory-task precedent.

**Objective gates:**
- Observed `intent` in evidence equals the injected sentinel (routing-workaround
  comparison identity, `env_flags.routing_workaround = "sentinel_intent"`)
  (`tests/verify.py:146-159`, `check_sentinel_routing`).
- `execute_code` is DESTRUCTIVE (`tools.py:1076-1082`) and fires the
  root-graph `interrupt_node`; pre-approval mock-event snapshot shows no
  execute request reached the E2B double before approval, exactly one sandbox
  created (`tests/verify.py:171-202`, `check_hitl_ordering`).
- Tool result `exit_code == 0`; `stdout` contains the SHA-256 digest of
  `"nous-benchmark-1101"`, independently recomputed by the verifier and
  asserted against both `stdout` and the final assistant message — grounding
  without a judge (`tests/verify.py:226-259`, `check_execution_result`).
- Execution count ≤ `MAX_EXECUTIONS_PER_RUN` (5) (`e2b_sandbox_manager.py:53`).
- No unsanctioned destructive tool executes (`tests/verify.py:202-226`).

**Coverage note:** `execute_code` is a **production dead tool in the pinned
benchmark image** (`registry.digitalocean.com/ragsystemregistry/backend:3a436b2-r1`)
— it is not reachable via live intent classification under any research/kg
turn (subgraph routing binds by subgraph; `execute_code` has `subgraphs=∅`,
`tools.py:1076-1082`). This task reaches it only via the sentinel-intent
`aupdate_state` workaround, recorded as an `env_flags` comparison identity,
not a claim of live reachability. A routing fix restoring `execute_code` to a
subgraph binding is landing on `develop` in **PR #1365**, but the pinned
benchmark image (`3a436b2-r1`) predates it — when the suite re-pins to an
image containing the fix, switch this task to live classification and
re-record. The E2B double genuinely executes submitted Python in a
subprocess inside the mock container (real stdout for deterministic code);
if the wire-protocol spike had found the SDK's streaming/auth handshake
unreproducible, the binding fallback is an honest env-gate (verifier exit 2,
infra-degraded, never a fake pass) — not exercised here since the double
proved feasible.

**Semantic gate:** N/A (design doc §Layer B) — counts as pass. Correctness is
the recomputed-digest grounding check above, not a free-text judgment.

**Status:** NOT YET GATED — awaits first recorded run.

### 17. External databases — current contract (2026-08-09)

The task now sends its connector-discovery/search instruction through live
classification. Explicit connector tool names deterministically route to
`general`, where both connectors are bound by intent. The verifier rejects the
retired sentinel, proves a fresh matching human turn was appended, and retains
the connector-inventory and anti-fabrication gates. The task remains
calibration-only until five recorded trials complete without infrastructure
failures.

### 17. Historical pre-#1378 contract

**Preconditions:** UUID block `12xx` — org `00000000-0000-4000-8000-000000001201`,
user `…001202`, workspace `…001203`, thread `…001204`. `search_external_database`
and `list_external_databases` both have `intents=∅`, `subgraphs=∅`
(`tools.py:1083-1096`) — unreachable via any live classification, general
included. This task drives the turn via the same sentinel-intent `aupdate_state`
workaround as capability 16.

**Objective gates:**
- Observed `intent` equals the injected sentinel
  (`tests/verify.py:156-169`, `check_sentinel_routing`).
- Neither tool is destructive; no HITL interrupt occurs — a paused graph is
  itself a gate failure (`tests/verify.py:180-194`, `check_no_hitl`).
- `list_external_databases`: `total == 11`; `fred.available == true`;
  `alpha_vantage` and `cosmic` `available == false` with
  `requires_api_key == true` (only the 3 key-gated connectors, per
  `base.py:109-116` + registry — corrects the design doc's "9 unavailable")
  (`tests/verify.py:194-238`, `check_list_external_databases`).
- Both searches execute with `connector` explicit, `max_results ≤ 20` (impl
  cap, `tools_impl.py:2997`); result rows ⊆ fixtures, `content` truncated to
  300 chars (`tools_impl.py:3052`); the FRED event carries the benchmark
  `api_key` query param (`tests/verify.py:238-283`, `check_search_results`).
- Mock events: no query the double never served; final message's claims are
  contained in the doubled results — deterministic containment, not judged
  (`tests/verify.py:283-318`, `check_mock_events` / `check_final_message`).

**Coverage note:** `search_external_database` and `list_external_databases`
are **production dead tools in the pinned benchmark image** — both have
`intents=∅` and `subgraphs=∅` (`tools.py:1083-1096`), so neither the general
path's per-intent binding nor any subgraph ever exposes them under live
classification. This task reaches them only via the sentinel-intent
`aupdate_state` workaround (`env_flags.routing_workaround = "sentinel_intent"`),
the same mechanism and same product gap as capability 16. The routing fix
restoring these tools is landing on `develop` in **PR #1365**; the pinned
benchmark image (`3a436b2-r1`) predates it. When the suite re-pins to an image
containing the fix, switch this task to live classification and re-record.
Only 2 of 11 connectors (`pubmed`, `fred`) are doubled; the remaining 9
connectors (7 keyless, 2 key-gated: alpha_vantage, cosmic) are deliberately
not exercised — the instruction pins
`connector=` explicitly to avoid an un-doubled fan-out via the no-connector
`list_available()` default (`tools_impl.py:3026`).

**Semantic gate:** N/A (design doc §Layer B) — counts as pass. Correctness is
the deterministic fixture-containment check above, not a free-text judgment.

**Status:** NOT YET GATED — awaits first recorded run.

### 18. External-harness MCP discovery

**Agent under test:** not the NOUS graph. Harbor's built-in `claude-code`
and `codex` agents run their CLIs in the task container and reach NOUS only
through the task's stdio MCP server, which execs the real
`packages/harness-bridge` (`nous-harness mcp`) against `mock-nous`, a
deterministic double of `GET /api/v1/integrations/tools` and
`POST /api/v1/integrations/tools/read`. The double's catalog is generated from
`read_tools.list_read_tools(["tools:read"])` and checked for drift by
`environment/mock_nous/generate_catalog.py --check`. Spec:
`evals/specs/harness-mcp-discovery-v1/`.

**Preconditions:** project-scoped `tools:read` catalog (13 tools); two planted
items per source with unique identifiers: library documents
`4c0e7a52-…3a19`/`9b3d1f68-…5d73`, arXiv `2509.17001`/`2509.17002`, PubMed
PMIDs `40517001`/`40517002` (DOIs `10.5555/nouseval.2025.*`). Model
credentials come from the host running Harbor (`ANTHROPIC_API_KEY` for
`claude-code`, `OPENAI_API_KEY` for `codex`), never from `task.toml`.

**Objective gates** (`tests/verify.py`, exit 10 on failure):
- Every integration request came from the bridge (User-Agent `node`); any
  other client bypassed the MCP primitives.
- The double accepted (HTTP 200) at least one bridge `search_documents`,
  `search_arxiv` and `search_external_database` call with a non-empty `query`.
- `/app/answer.md` exists, is a regular singly linked file (never read
  through a symlink) and is not blank.
- The answer cites a planted library document (id or exact title), a planted
  arXiv id, and a planted PMID or DOI.
- No fabricated identifier: every arXiv-style id, PMID (labelled, listed,
  PubMed URL, or standalone on a PubMed/PMID line or table), PMCID, DOI and
  UUID in the answer was served by the double.
- Every paper-list row names a served identifier or exact served title.

**Infrastructure gate** (exit 2, no reward): double unreachable; no successful
bridge `GET /api/v1/integrations/tools` (the MCP server never connected); a
bridge request without both `Authorization` and `X-NOUS-Integration-Grant`.

**Semantic gate:** N/A (no judge) — counts as pass.

**Calibration:** `pass.json` (exit 0); `wrong-missing-external.json`,
`wrong-fabricated-id.json`, `wrong-fabricated-doi.json`,
`wrong-uncited-row.json`, `wrong-symlinked-answer.json`,
`wrong-bypassed-bridge.json` (exit 10); `infra-no-catalog.json` (exit 2).

**Coverage note:** this capability measures the external-harness MCP surface,
not the agent graph, so this file's manifest keys apply with substitutions:
the harness is Harbor 0.6.6's built-in agents plus the CLI versions Harbor
records per trial (not `evals/harbor_agents/*.py`), and `env_flags` are N/A
because no backend process runs (record the harness name, model and CLI
version instead). Its first use is the pre-registered Plan 07 Slice 5
decision (`specs/harness-mcp-discovery-v1/harness.md`: five scoreable trials
per harness; primitives suffice iff every harness passes at least 4 of 5).
That product decision rule is not this suite's gate; promoting the task to
GATED still needs 5/5 objective passes with zero infrastructure failures
under the go/no-go threshold above.

**Status:** NOT YET GATED — task landed, calibration verified locally (8/8
expected exit codes), awaits first recorded run. See
`source-manifests/agent-flow-2026-10-07-harness-mcp-discovery.json`.

**Sequencing note:** "next" tier = highest defect-density areas by repo
history (fake-success writers, HITL, tenant scope, arXiv ingest). Build one
Harbor task per capability; do not batch — each task needs its own digest,
calibration fixtures (1 pass + 1 wrong), and manifest row before it can gate.

## Benchmark hygiene

### Pre-run

- Delete all synthetic user's projects, threads, Redis keys, active traces.
- Verify DB migration state matches `alembic current` at `repo_sha`.
- Confirm verifier calibration fixtures pass (1 pass + 1 wrong per gated task × 3 tasks = 6/6).
- Record manifest block, including `env_flags`.

### Post-run

- Assert no *unexpected* leftover state beyond what the task's success condition requires.
  (e.g., the direct-action task expects one project to exist — that is expected state, not a leak.)
- Expected-state allow-list includes: the task's declared postcondition rows,
  and the stream replay list key under its 3600 s TTL (cancellation task).
- Unexpected rows, Redis keys, or active traces for the synthetic user fail as objective-gate violations.
- Cleanup (teardown) is a harness responsibility and runs after verification.

### Artifact capture (per trial)

Every trial archives to `evals/jobs/<run_id>/`:

| Artifact | Format |
| --- | --- |
| Raw SSE events | `.jsonl` |
| Agent payloads (tool calls, responses) | `.json` |
| DB snapshot (synthetic user rows) | `pg_dump --data-only` filtered |
| Redis snapshot (synthetic keys) | `DUMP` or JSON export |
| Judge prompt + response | `.json` |
| Command used | `command.sh` |
| Manifest block | `manifest.json` |

### Baseline revision rule

If `task_digest`, `harness_digest`, or `env_flags` changes, the old baseline is
archived to `evals/baselines/agent-flow-<date>.json` and a new one is declared.
Never compare across digest boundaries without explicit `--force-new-baseline`.

## Historical context (2026-08-04 baseline)

The initial baseline scored **1/3** and exposed three P1 defects, all fixed in
PR #1350:

1. `_TOKEN_RE` covered only `ghp_`/`ghs_` — `gho_` tokens survived redaction.
2. `persist_partial_stop()` discarded the id from `_persist_assistant_message_safe()`.
3. Token buffer appended before `emitter.emit()` await — cancel could persist unseen chunks.

Defect 1 is unit-regression-tested (regex level; the end-to-end retrieval path
is gated here but only enforced on the DO KB branch — see capability 2 known
gaps). Defects 2–3 are regression-tested on the graph stream path only; the
fast path retains both defect shapes (see capability 3 preconditions). The
2026-08-04 raw data remains in `evals/baselines/agent-flow-2026-08-04.json`.

## Open implementation gaps (from the 2026-08-07 source audit)

All five closed by PR #1353 (kept for history):

1. ~~Postgres hybrid-fallback contexts and `search_documents` results bypass
   `redact_pii`~~ — fixed in #1353: both paths redact before the model
   boundary (hybrid redacts before the 3000-char slice).
2. ~~Luna fast path: persist-before-emit ordering and discarded persisted-row
   id on cancel~~ — fixed in #1353: fast path now matches the graph path's
   prefix + linkage invariants. (Pre-existing residual, noted in the fast-path
   cancel test: the outer route-agnostic handler issues a second unlinked
   `run.cancelled` finalize, absorbed by `uq_agent_run_events_one_terminal`.)
3. ~~`AgentRun.assistant_message_id` column is dead~~ — fixed in #1353:
   `finalize_submission` projects the id from terminal payloads onto the run
   row (UUID-guarded).
4. ~~`score_source` dropped from `AgentExecuteResponse.retrieved_contexts`~~ —
   fixed in #1353. Note: the field surfaces in runtime job-result payloads and
   the SSE `rag.context` frame; it does NOT appear in `openapi.json` because
   `AgentExecuteResponse` is not any route's `response_model` (job-based API).
   Still not persisted on citations (accepted).
5. ~~Weak-but-nonzero keyword hits override sub-0.7 LLM verdicts~~ — fixed in
   #1353: a specialized LLM verdict ≥ 0.60 that is more confident than the
   keyword hit now wins; ties and stronger keyword hits keep the keyword
   result.

## Comparison contract

For any future revision:

1. Run each gated capability ≥ 5 trials (3 canonical + 2 near-boundary) with matching digests and `env_flags`.
2. Report per capability:
   - Objective: N/5 pass. Semantic: N/5 pass or N/A. Infra failures: count.
   - p50/p95 elapsed, first-token latency, terminalization time.
   - Input/output/cache tokens.
   - Intent, confidence, tool sequence, loop count, tool-call count, retry count, terminal reason.
3. Report aggregate:
   - Suite score = capabilities where (objective 5/5) ∧ (semantic ≥ 4/5 or N/A).
   - Infra health = runs with zero C-failures / total runs.
4. Gate rule: merge requires suite score = 3/3 (gated capabilities only) and infra health = 100%.
5. Evidence in `evals/jobs/`; machine-readable summary in
   `evals/baselines/agent-flow-<date>.json`; source manifest in
   `evals/source-manifests/agent-flow-<date>.json`.

## Recorded amendment: capability 18, 2026-10-16

The first accepted series is recorded in
`baselines/agent-flow-2026-10-16-harness-mcp-discovery-http.json` and its dated
source manifest. Codex 0.161.0 / Azure `gpt-5.6-terra`: objective **1/5**,
semantic **N/A**, accepted-series infrastructure failures **0**; five earlier
transport-degraded attempts were excluded under the registered rule. The
Codex-only product decision is **PRIMITIVES INSUFFICIENT**. This is historical
primitive composition at bridge revision `57fc21bc7`, not current composite
quality or deployment acceptance. Status remains **NOT GATED**. The earlier
"awaits first recorded run" statements are historical pre-registration state.
No prior baseline has been replaced or scored across a digest boundary.

### Capability 18: corrected-task record

Mandatory typing corrections produced a new task/verifier digest. The fresh
record `baselines/agent-flow-2026-10-16-harness-mcp-discovery-typed-http.json`
contains 0/5 objective passes, semantic N/A, zero infrastructure failures,
and the Codex-only PRIMITIVES INSUFFICIENT decision. It supersedes no prior
record and reuses no score. Five attempts were used at this digest; total
across task versions is fifteen. Status: NOT GATED.

### Capability 18: verifier correction and fresh decision, 2026-10-17 UTC

Later verifier defects invalidate using the earlier rewards for current
acceptance. Those historical records remain unchanged. Successful results,
per-trial returned identifiers, every paper-list block and answer truncation
are now checked by the corrected verifier. All 13 in-image calibration cases
matched before the new series.

The separate [verified HTTP record](baselines/agent-flow-2026-10-16-harness-mcp-discovery-verified-http.json)
ran at clean source `5f682a33`, with the real bridge at `57fc21bc7`, Harbor
0.6.6 and Codex 0.161.0 / Azure gpt-5.6-terra. Seven attempts produced five
scoreable trials with **0/5 objective passes**, **N/A semantic**, and **two
infrastructure exclusions**. A recovered provider-stream failure invalidated
one raw passing reward; an agent-install failure emitted no reward. Two
unchanged replacements completed the series. The fresh registered decision
is **PRIMITIVES INSUFFICIENT for Codex only**. No old score is reused or pooled;
the campaign now retains 22 attempts across versions.

Status remains **NOT GATED**. Build-export digests were recovered for all
attempts, while direct container snapshots exist only for the replacements.
Agent phrasing/order variants and Fable benchmark trials were not run.
Current composite quality, real-provider ranking and deployed acceptance are
separate from this historical primitive-surface decision. See the
[evidence and limits](../docs/testing/evidence/discovery-harness-verified-20261016/README.md).
