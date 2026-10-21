# NOUS tool-coverage benchmark — design

Date: 2026-08-07
Status: approved (brainstorm), pending implementation plan
Predecessor: `evals/AGENT_FLOW_BASELINE.md` (2026-08-07 regression gate, PR #1352); pairs with P1 fixes in #1350.

## Goal

End-to-end Harbor benchmark coverage for all 23 tools in `TOOL_REGISTRY`
(`backend/src/services/agent/tools.py:864-1103`), grouped into 8 new Harbor
tasks that follow the full #1352 regression-gate conventions: 3-layer scoring
(A objective / B judge / C infra), 5 trials per capability (3 canonical + 2
near-boundary), immutable manifest + digest validation, calibration fixtures
(1 pass + 1 wrong per task).

Baseline rule honored (`AGENT_FLOW_BASELINE.md:333-336`): one Harbor task per
capability, each with its own digest, calibration fixtures, and manifest row.
Tasks slot into the baseline's existing coverage roadmap (capabilities 5-14)
rather than forming a parallel suite.

## Task inventory

| # | Task id | Roadmap cap | Tools covered | Env services beyond Postgres |
|---|---------|-------------|---------------|------------------------------|
| 1 | `agent-arxiv-research-flow-v1` | 5 | `search_arxiv`, `ingest_arxiv_papers` | arXiv API double (Atom XML + PDF bytes), local object storage, HITL |
| 2 | `agent-writing-flow-v1` | 6 | `create_draft`, `export_bibliography`, `compare_documents` | seeded corpus; judge |
| 3 | `agent-knowledge-graph-flow-v1` | 7 | `search_knowledge_graph`, `explore_entity_neighborhood`, `find_entity_paths`, `get_graph_stats`, `extract_entities` | real `neo4j:5` container, seeded deterministic graph; judge |
| 4 | `agent-memory-roundtrip-v1` | 9 | `forget_memory` (+ store write path) | pgvector LangGraph store |
| 5 | `agent-project-management-v1` | 14 | `create_project`, `list_projects`, `add_document_to_project`, `create_project_note`, `list_project_documents` | none |
| 6 | `agent-kb-retrieval-v1` | new | `search_documents`, `do_kb_retrieve`, `summarize_document` | mock-services double (rag-task pattern); judge |
| 7 | `agent-code-execution-v1` | new | `execute_code` | E2B wire-protocol double |
| 8 | `agent-external-databases-v1` | new | `search_external_database`, `list_external_databases` | connector double (PubMed + FRED shapes; others listed unavailable) |

Tool-to-task assignment follows routing reality, not topic aesthetics:
`compare_documents` is writing-subgraph-only; `extract_entities` carries the
`knowledge_graph` intent (data subgraph position 0); `execute_code`,
`search_external_database`, `list_external_databases` are bound to **no
subgraph** and are reachable only through the unknown-intent `ALL_TOOLS` path
(`_nodes_llm.py:101-148`) — tasks 7 and 8 engineer instructions that classify
to no specialized intent, and Layer A asserts the observed
`intent`/`classifier_source` to prove the path. `summarize_document` rides
task 6 (search → summarize is the natural trajectory); its subgraph binding is
verified at implementation time and the task's intent engineered accordingly.

## Per-task shape

Every task is a self-contained dir cloned from the closest existing pattern:

- Graph-level tasks (1-5): clone `agent-direct-project-action-v1`
  (`run_agent.py` drives `compile_agent_graph` + `astream`, resumes HITL
  interrupts with `Command(resume={"confirmed": True})`).
- Protocol-double tasks (6-8): clone `rag-retrieval-safety-grounding-v1`
  (mock service records every request as tamper-proof evidence via
  `GET /events`).

Required per task: fixed UUID block (next free `…05xx`+ ranges),
`EXPECTED_INSTRUCTION` triple-declared (instruction.md, run_agent.py,
verify.py), `task.toml` with `[metadata].benchmark_id` etc., `.gitignore`,
`tests/{verify.py,test.sh}` with the 0/10/other reward triad, calibration
`pass.json` + `wrong-<mode>.json`, and a `specs/<task>/` triple
(`task.md`/`environment.md`/`harness.md` in the strict `Label:` paragraph
format, `Status: approved` on line 1, no markdown headings).

Compose topology per existing pattern: `benchmark-internal` (internal) +
`egress-public` networks; `main` only on internal; Squid `egress-proxy` sole
bridge, allowlisting exactly the model host; `allow_internet = true` with the
standard rationale comment; healthchecked `depends_on`.

## Scoring

- **Layer A (deterministic)** per task: identity gates (benchmark_id,
  source_revision, instruction), network-boundary triple (+ per-task private
  probes), tool-call sequence and args, HITL interrupt/approval ordering with
  no-mutation-before-approval, independent psycopg/redis/neo4j state reads
  cross-checked against adapter evidence, impl-level caps (impl caps win over
  wrapper caps: e.g. `search_arxiv` 5, `list_projects` 50,
  `compare_documents` 5), loop ceilings (research 5 / writing 8 / data 8),
  `TOOL_ERROR_HINTS` classification firing on near-boundary error trials, and
  `make_filtered_tool_node` block messages as negative assertions where a tool
  must NOT be reachable.
- **Layer B (judge)** only where semantic output exists: task 2 (draft +
  comparison quality), task 3 (entity-answer synthesis), task 6 (grounding +
  summary quality).
  Tasks 1, 4, 5, 7, 8 record `semantic = N/A` (counts as pass). Judge follows
  the rag pattern: `HARBOR_JUDGE_*` in `[verifier.env]` only, calibration
  before validity, strict JSON verdict, injection-defended prompt.
- **Layer C (infra)**: adapter exit 70 / verifier exit ∉ {0, 10}; no reward
  file emitted; run flagged `infra-degraded`, retry before merge.
- Thresholds per baseline: objective 5/5, semantic ≥4/5, zero infra failures.

## Trials

3 canonical (fixed seed) + 2 near-boundary per task. Near-boundary trials
deliberately hit failure/recovery paths, one per task minimum: missing
`project_id` (→ `_missing_project_error`, `suggestion: list_projects`),
arXiv-id-passed-as-document-id (recoverable hint), empty KG result, >10
ingest batch rejection, E2B timeout, unknown connector name, forget_memory
empty-match, invalid `workspace_id`.

## New service doubles

| Double | Fidelity | Notes |
|--------|----------|-------|
| Neo4j | real `neo4j:5` container on internal network, seeded ~20-entity deterministic fixture graph | not a mock: prod driver, circuit breaker, 15 s `wait_for` all exercised. Seed via adapter using prod `knowledge_graph_service` write path or direct cypher fixture |
| E2B | HTTP wire double of sandbox create/exec/kill | most speculative; SDK protocol must be reproduced. Fallback: if double proves infeasible, task env-gates to `infra-degraded` rather than fake-passing |
| arXiv | Atom XML query responses + deterministic PDF bytes | proxied via internal mock host; also validates Redis cache keys `arxiv:search:*` TTL behavior |
| Connectors | PubMed + FRED protocol shapes | remaining 9 connectors asserted as listed-but-unavailable by `list_external_databases` |

All doubles record request events (rag `server.py` pattern) so verifiers gate
on what the agent actually sent (auth headers, arg shapes, dedup/redaction).

## Common module — `evals/harbor_common/`

Hoist before writing 8 new tasks (identified drift candidates across the 3
existing tasks): `proxy/` (byte-identical ×3), `test.sh` template, `json_safe`,
`validate_network_boundary(extra_probes=…)`, `InfrastructureFailure` +
`run_adapter(...)` envelope (exit 70, evidence/trajectory write, cleanup),
`bootstrap_schema` (`Base.metadata.create_all`, Alembic-broken rationale),
`seed_tenant(...)`, `collect_model_usage` / `final_assistant_message`,
`build_atif_trajectory(...)`, verifier `main()` envelope (0/10/2 mapping,
audit.json), `load_inputs()` calibration switch with unified fixture envelope
`{"evidence": …, "state": {…}}`, compose base fragment (postgres +
egress-proxy + networks + shared env) via Compose `include:`/anchors, shared
Dockerfile stanza (numpy repair, chmod/chown, ENTRYPOINT []).

Kept duplicated on purpose: verifier state reads and SSE parsing (independence
is the cross-check), truth/calibration fixtures outside images.

Existing 3 tasks are NOT migrated to the common module in this effort — their
digests are pinned to recorded evidence. Migration happens at next baseline
revision.

Caution: duplicate `run_agent.py` basenames are a known pytest-collection
hazard (`test-pipeline.yml:111`); mypy added-file gate already excludes
`evals/` (`test-pipeline.yml:103-106`).

## Environment flags per task

- Task 3: real `NEO4J_URI` to the container (others keep `bolt://127.0.0.1:1`).
- Task 4: memory store enabled with pgvector embedder or unranked
  substring-match fallback pinned explicitly — flag choice recorded in
  `env_flags` (comparison identity).
- Task 6: `DO_KB_ENABLED/PRIMARY_READ=true`, mock hosts, rerank on (rag
  pattern).
- Task 7: `E2B_API_KEY` set to benchmark token, SDK base URL pointed at the
  double.
- Tasks 7/8: instructions engineered for the unknown-intent path;
  `AGENT_PARALLEL_TOOL_CALLS=false` as in existing tasks.
- `load_project_skill` and Luna fast path are explicitly OUT of scope (roadmap
  caps 13 and skill-runtime remain `later`-tier; `load_project_skill` needs
  `PROJECT_SKILL_RUNTIME_ENABLED` + runtime snapshot + catalog state —
  deferred to its own capability task).

So covered: 22 of 23 registry tools now, `load_project_skill` deferred with
rationale recorded in the baseline roadmap table.

## Docs & manifest updates

- `evals/AGENT_FLOW_BASELINE.md`: promote roadmap entries 5, 6, 7, 9, 14 to
  full gated capability spec sections (the way #1352 promoted intent routing)
  and add the three new capabilities; keep heading/format conventions exactly
  (manifest table key order, `**Objective gates:**` bullets with `file:line`
  anchors, trial table, go/no-go thresholds).
- New dated `evals/source-manifests/agent-flow-<run-date>.json` with
  `task_digests` for all 11 tasks (digest algorithm: SHA-256 of sorted
  `shasum -a 256` lines, repo-relative paths, `__pycache__` excluded).
- New dated `evals/baselines/agent-flow-<run-date>.json` with
  `verifier_calibration` (11 × {pass, wrong}) and `benchmarks[]` rows.
- `evals/README.md`: extend the task table + run commands.

## Out of scope

- `load_project_skill` capability task (own effort; runtime-snapshot fixture
  needed).
- CI wiring of `harbor run` (baseline gate stays a manual merge contract; the
  existing `agent-eval.yml` LangSmith sweep is untouched).
- Migrating the 3 existing tasks onto `harbor_common` (digest-pinned).
- HITL interrupt lifecycle, long-run controls, tenant isolation probes,
  error-recovery capability (roadmap 8, 10, 11, 12) — remain `later` tier.

## Risks

1. E2B double protocol fidelity — mitigated by env-gated `infra-degraded`
   fallback; spike first.
2. Neo4j deterministic seeding — entity extraction is LLM-driven in prod, so
   the fixture graph is seeded directly (cypher), not via `extract_entities`;
   `extract_entities` output is judged (Layer B), not diffed.
3. Unknown-intent routing for tasks 7/8 is classifier-behavior-dependent —
   near-boundary trials specifically probe phrasing robustness; if the
   classifier routes to a subgraph, Layer A catches the
   `make_filtered_tool_node` block and the trial fails loudly.
4. Volume: ~10-12k lines across 8 tasks + common module. Sequenced
   implementation (common module → clone-pattern tasks → double-dependent
   tasks) keeps each PR reviewable.

## Status update 2026-10-17

The original status ("approved (brainstorm), pending implementation plan") is kept as written. Checked on 2026-10-17: implementation plans 2 to 5 landed as #1360 (judge helper, writing flow, KB retrieval), #1362 (knowledge-graph flow, memory round trip), #1366 (arXiv, code execution, connectors) and #1376 (HITL lifecycle, tenant isolation, fast path). Plan 1 was not matched to a merge commit by subject, so its status is unverified here. Current benchmark behavior is defined by the code under `evals/`.
