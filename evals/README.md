# Agent evaluations

This directory contains production-flow Harbor benchmarks for the deployed NOUS
agent and the older coding-agent suites.

## Production agent-flow benchmarks (Harbor)

These tasks execute pinned production code against isolated PostgreSQL, Redis,
and deterministic service doubles. The main task container has no unrestricted
egress: a Squid sidecar permits only the configured model host, and every task
records positive and negative network probes.

| Task | Capability | Primary verifier |
| --- | --- | --- |
| `agent-direct-project-action-v1` | Route, confirm, and execute a direct project action | Deterministic trajectory plus PostgreSQL state |
| `rag-retrieval-safety-grounding-v1` | Safely postprocess, rerank, and synthesize noisy KB results | Deterministic safety/provenance gates plus isolated semantic judge |
| `agent-stream-cancel-durability-v1` | Make client Stop terminal and durable at the production SSE API | Deterministic SSE, PostgreSQL, Redis, and resume checks |
| `agent-project-management-v1` | Route, approve, and execute a multi-step project-management flow | Deterministic trajectory, HITL snapshots, and PostgreSQL state |
| `agent-writing-flow-v1` | Compare documents, draft (HITL, async fake-success trap), and export a bibliography | Deterministic trajectory, HITL snapshots, PostgreSQL state, and an isolated semantic judge |
| `agent-kb-retrieval-v1` | Search indexed documents, then retrieve and cite grounded guidance from the org knowledge base | Deterministic trajectory, mock-KB auth/network probes, and an isolated semantic judge |
| `agent-knowledge-graph-flow-v1` | Search, explore neighborhood, and pull stats from a real Neo4j knowledge graph, tenant-scoped | Deterministic trajectory, independent Cypher cross-check, and an isolated semantic judge |
| `agent-memory-roundtrip-v1` | State a fact, recall it next turn, then `forget_memory` (HITL) and prove it's gone | Deterministic trajectory, redaction and no-bleed checks, PostgreSQL store state (no judge — semantic N/A) |
| `agent-arxiv-research-flow-v1` | Search arXiv against a protocol double, prove the shared Redis result cache, and ingest two papers behind one HITL approval | Deterministic trajectory, HITL snapshots, PostgreSQL/Redis/storage state |
| `agent-code-execution-v1` | Execute Python against an E2B wire double behind one HITL approval | Deterministic trajectory, HITL snapshots, recomputed-digest grounding (no judge) |
| `agent-external-databases-v1` | List and search the 11-connector external-database registry against a protocol double | Deterministic trajectory, connector-registry state, fixture-containment check (no judge) |
| `agent-error-recovery-v1` | Follow recoverable tool guidance, then stop repeated failures honestly | Deterministic error/counter gates plus an isolated semantic judge |
| `agent-long-run-controls-v1` | Compact a sequential retrieval chain and synthesize truthfully at the loop limit | Deterministic chain, compaction, ledger, and final-answer gates |
| `agent-project-skill-runtime-v1` | Freeze an approved project-skill version across activation drift and HITL resume | Deterministic version/hash, snapshot, HITL, and PostgreSQL state |
| `agent-fast-path-cancel-v1` | Keep fast-path cancellation terminal and prefix-safe | Deterministic stream and durability checks |
| `agent-hitl-lifecycle-v1` | Preserve confirm, reject, timeout, and replay semantics | Deterministic interrupt, mutation, and idempotency checks |
| `agent-tenant-isolation-v1` | Reject cross-tenant probes across agent read paths | Deterministic tenant-state and leakage checks |

GOO-293's current-writing evaluation uses
[`academic-writing-baseline-v1`](academic-writing-baseline-v1/README.md). That
protocol preserves the August 8 result, freezes five canonical trials plus
separate development and held-out near-boundary cases, and validates retained
draft, review, citation and download artifacts. It reports objective, semantic,
authorization, infrastructure and judge-unavailable outcomes separately; its
collector does not execute or score a provider by itself.

`agent-writing-flow-v1`, `agent-kb-retrieval-v1`, and
`agent-knowledge-graph-flow-v1` are judge-gated: Layer B
(`harbor_common.judge.run_semantic_judge`) only runs after every Layer A
(deterministic) check passes. `agent-memory-roundtrip-v1` is deterministic-only
(semantic N/A). `agent-writing-flow-v1` and `agent-kb-retrieval-v1` took their
first recorded run on 2026-08-08 (5 trials each,
`baselines/agent-flow-2026-08-08-writing-kb.json`): `agent-kb-retrieval-v1`
passed 5/5 and is now **GATED**; `agent-writing-flow-v1` failed 5/5 at Layer B
and stays **NOT GATED** — `compare_documents` states domain claims that neither
source document supports, so the grounding rubric fires on every trial.
`agent-knowledge-graph-flow-v1` and `agent-memory-roundtrip-v1` are NOT YET
GATED in `AGENT_FLOW_BASELINE.md` — awaiting their first recorded run.
`agent-knowledge-graph-flow-v1` additionally requires a real `neo4j:5`
container (see its `docker-compose.yaml`), not a mock.

The judge needs `HARBOR_JUDGE_ENDPOINT`, `HARBOR_JUDGE_API_KEY`,
`HARBOR_JUDGE_MODEL`, and `HARBOR_JUDGE_API_VERSION`. They are declared in
`[verifier.env]` only and must never appear in `[environment.env]`, or the
agent under test could reach the judge. Use a `gpt-4.1`-class deployment: gpt-5
deployments fail the Azure Responses API version gate.

`agent-arxiv-research-flow-v1`, `agent-code-execution-v1`, and
`agent-external-databases-v1` are all semantic = N/A (no judge anywhere in
these three tasks — design doc §Layer B) and all NOT YET GATED, awaiting
their first recorded run. `agent-code-execution-v1` and
`agent-external-databases-v1` send real turns through production intent
classification; see `AGENT_FLOW_BASELINE.md` capabilities 16/17 for their
routing and grounding contracts.

Tasks added after 2026-08-07 import their adapter and verifier helpers from the
shared `harbor_common/` package; the original three tasks keep their inline
copies and stay pinned by their existing digests.

The approved capability, Environment, and Harness contracts are under `specs/`.
The pinned baseline and audit are in `baselines/agent-flow-2026-08-04.json` and
`AGENT_FLOW_BASELINE.md`. Generated trial evidence is intentionally ignored by
Git but retained locally under `jobs/` until the evaluation is accepted.

Run a task from the repository root with Harbor 0.6.6 and approved model/judge
environment variables already present. The private production dependency image
`registry.digitalocean.com/ragsystemregistry/backend:3a436b2-r1` must also be
available locally or pullable with registry authentication; its expected digest
is recorded in each `task.toml`:

```bash
harbor run \
  --path evals/agent-direct-project-action-v1 \
  --agent-import-path evals.harbor_agents.nous_production_agent:NousProductionAgent \
  --env docker \
  --jobs-dir evals/jobs \
  --job-name agent-direct-project-action-v1-<revision> \
  --force-build \
  --n-concurrent 1 \
  --yes
```

Replace the task path and job name for the other benchmarks (including
`agent-writing-flow-v1` and `agent-kb-retrieval-v1`). Do not reuse a
prior score after the task digest, repository revision, or Harness digest in
`source-manifests/` changes. An adapter, dependency, credential, reset, timeout,
judge, or verifier failure is infrastructure and must not be scored as agent
reward 0.

## Coding-agent suites

Two legacy suites benchmark coding agents against the NOUS agent v2 system.

### 1. V2 Module Implementation (`tasks/v2-modules/`)

Tests whether an agent can correctly implement each v2 module **from the plan spec**.
Pinned to commit `6af5432` (pre-v2 — only the design doc and plan exist, no implementation).

Each task gives the agent the design doc as context + a focused prompt.
Because `6af5432` predates the dedicated v2 test files, each judge pulls the
matching test file from commit `98a17bb` via `git show` into a temporary file
before running `pytest`.

| Task                  | Module            | Judge           |
| --------------------- | ----------------- | --------------- |
| `error-recovery.yaml` | error_recovery.py | 10 pytest tests |
| `classifier.yaml`     | classifier.py     | 24 pytest tests |
| `compactor.yaml`      | compactor.py      | 24 pytest tests |
| `planner.yaml`        | planner.py        | 6 pytest tests  |
| `reflection.yaml`     | reflection.py     | 9 pytest tests  |
| `memory-store.yaml`   | memory_store.py   | 9 pytest tests  |

### 2. Regression (`tasks/regression/`)

Tests whether an agent can make changes to the v2 system **without breaking existing tests**.
Pinned to commit `98a17bb` (full v2 implementation, 145-test regression suite).

| Task                      | Scenario                    | Judge                         |
| ------------------------- | --------------------------- | ----------------------------- |
| `add-tool.yaml`           | Add a new agent tool        | 145 existing + new tests      |
| `modify-classifier.yaml`  | Add a new intent category   | All classifier tests pass     |
| `extend-error-hints.yaml` | Add new error hints         | All error recovery tests pass |
| `refactor-compactor.yaml` | Change compaction threshold | All compactor tests pass      |

### Usage

```bash
# Install agent-eval (see https://github.com/joaquinhuigomez/agent-eval)

# Run a single task
agent-eval run --task evals/tasks/v2-modules/error-recovery.yaml --agent claude-code --runs 3

# Run all v2 module tasks
agent-eval run --task evals/tasks/v2-modules/ --agent claude-code --agent aider --runs 3

# Run regression suite
agent-eval run --task evals/tasks/regression/ --agent claude-code --runs 3

# Generate report
agent-eval report --format table
```
