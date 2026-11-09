# Tool-Coverage Benchmark Plan 3: knowledge-graph-flow + memory-roundtrip

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship the two remaining "own-service / stateful" capabilities — `agent-knowledge-graph-flow-v1` (capability 7: the five KG tools against a real Neo4j) and `agent-memory-roundtrip-v1` (capability 9: store-write → recall → `forget_memory`), calibration-verified.

**Architecture:** Task 1 (KG) is the first task backed by a **real neo4j:5 container** (not a mock) — prod driver, circuit breaker, and the tool-level 15s `wait_for` all exercised; the fixture graph is seeded by **direct cypher** (entity extraction is LLM-driven in prod, so `extract_entities` output is judged, never diffed). Task 2 (memory) is a **multi-turn** graph task over the pgvector LangGraph store: state a fact, recall it next turn, then `forget_memory` (a DESTRUCTIVE tool → one HITL approval) and prove it's gone. Both clone the graph-level adapter (`agent-direct-project-action-v1`); KG adds the judge + a neo4j double, memory adds nothing external (Postgres store, same container).

**Tech Stack:** Python 3.11, LangGraph agent graph (pinned image `registry.digitalocean.com/ragsystemregistry/backend:3a436b2-r1`, digest `sha256:75b224f8…c0e34`), neo4j:5, psycopg 3, Harbor 0.6.6 schema 1.2, Squid egress proxy, `AzureChatOpenAI` judge (KG only).

**Model roles:** Fable plans/reviews; **Sonnet** implements each task; **Opus** spec+quality review between tasks; **Fable** whole-branch final pass before PR.

## Dependency / stacking

This branch **stacks on plan 2** (`feat/tool-coverage-bench-plan2`, PR #1360) because Task 1 consumes `harbor_common/judge.py`, which lands in plan 2, not develop. Base the PR on `develop` only **after #1360 merges**; until then keep it stacked (PR base = feat/tool-coverage-bench-plan2, or rebase onto develop post-merge). Do not re-hoist the judge.

## Global Constraints

- Design doc `docs/superpowers/specs/2026-08-07-tool-coverage-benchmark-design.md` is **binding**. Task 3=cap 7, task 4=cap 9. Trials: 3 canonical + 2 near-boundary. Scoring Layers A/B/C.
- **Semantic scope (design doc §Layer B):** task 3 (KG) is **judge-gated** (entity-answer synthesis). task 4 (memory) is **`semantic = N/A`** (deterministic, counts as pass). The roadmap's cap-9 line mentions a semantic gate — the **design doc wins**; the promoted cap-9 section records `Semantic: N/A` and notes the reconciliation.
- `harbor_common` is consumed, never forked. Available: `envelope.{load_inputs,run_verifier_main,InfrastructureFailure}`, `db.{bootstrap_schema,seed_tenant,initial_agent_state}`, `network.validate_network_boundary(model_endpoint, extra_probes=…)`, `judge.run_semantic_judge`, `trajectory.build_atif_trajectory`, `serialization.{json_safe,utc_now}`, `templates/{test.sh,proxy/}`.
- Originals (3 pre-2026-08-07 tasks + plan-1 `agent-project-management-v1` + plan-2 `agent-writing-flow-v1`/`agent-kb-retrieval-v1`) are **digest-pinned — do not touch**.
- Spec files `evals/specs/<task>/{task.md,environment.md,harness.md}`: line 1 `Status: approved`, `Label:` paragraphs, no markdown headings. KG spec matches `rag-retrieval-safety-grounding-v1` label order (has Data/Storage/Fidelity); memory spec matches `agent-direct-project-action-v1`.
- Reward: verify.py exit 0→reward 1, 10→reward 0, else→infra (no reward file).
- Judge creds `HARBOR_JUDGE_*` in `[verifier.env]` ONLY (KG task).
- Fixed UUID blocks: **08xx** = KG, **09xx** = memory.
- `EXPECTED_INSTRUCTION` byte-identical across instruction.md, run_agent.py, verify.py. No `test_`-prefixed files in task dirs except `test.sh`.
- Impl caps are ground truth: `search_knowledge_graph` limit `min(arg,50)` (tools_impl.py:2460); `explore_entity_neighborhood` depth `min(arg,3)`/limit `min(arg,50)` (:2522); `find_entity_paths` depth `min(arg,5)`, paths `[:5]` (:2593); DATA loop ceiling 8 (data_agent.py:39).

## Landmines (READ before building — from recon, each cost real time elsewhere)

1. **neo4j singleton trap (cap-7 known trap).** Importing `knowledge_graph_service` binds the submodule, not the singleton — a dead hybrid-search path scores as C (infra). The adapter must drive the SAME singleton the tools use. Verify at build time; a KG run that returns empty because it hit a dead client is infra, not reward 0.
2. **`get_graph_stats` counts must match a direct Cypher count** (Layer A). The verifier does an independent `cypher` count of the seeded graph and cross-checks the tool's `total_entities`/`total_relationships`.
3. **Tenant scoping is the core cap-7 assertion.** Every KG query filters `e.organization_id = $org` (`_entity_scope_predicate`). Seed every node with the benchmark org UUID. A negative probe: a second org's entity must NOT appear in results.
4. **Memory write is fire-and-forget.** `memory_save_node` dispatches `_persist_memory_async` as a background task (`_nodes_memory.py:286-310`). The task-2 adapter MUST drain/await that task (or poll the store until the key lands) before the turn-N+1 recall assertion, or the roundtrip flakes.
5. **`forget_memory` is DESTRUCTIVE + CONTEXT_FREE, subgraphs=∅** → reachable only via the unknown-intent ALL_TOOLS path AND fires the root-graph `interrupt_node`. Task 2 engineers the instruction to that path and handles ONE HITL approval for the forget.
6. **Store backend is a comparison-identity flag.** No Cohere → unranked store → `forget_memory` uses the substring-match fallback (`memory.py:247-260`), not the 0.6 score threshold. Pin the choice (`ALLOW_MEMORY_FALLBACK`/Cohere-off) in `env_flags` and assert the path taken. `ENVIRONMENT=="testing"` forces `InMemoryStore` — do NOT set that; use the Postgres store like production, or pin the fallback explicitly.

---

### Task 0: Branch — DONE
Worktree `feat/tool-coverage-bench-plan3` created off plan-2 HEAD. Backend venv `/Users/goodwiinz/development/RAG_system/backend/.venv/bin`.

---

### Task 1: `agent-knowledge-graph-flow-v1` (capability 7)

**Files (`evals/agent-knowledge-graph-flow-v1/`):** `environment/{Dockerfile,docker-compose.yaml,run_agent.py,proxy/…, neo4j/}`, `instruction.md`, `task.toml`, `.gitignore`, `tests/{verify.py,test.sh,truth.json,calibration/{pass.json,wrong-<mode>.json}}`, `evals/specs/agent-knowledge-graph-flow-v1/{task.md,environment.md,harness.md}`.

**UUID block 08xx:** ORG `…000801`, USER `…000802`, WORKSPACE `…000803`, a SECOND org `…000810` (tenant-negative probe), entity/doc ids `…0804`–`…0809`.

**Flow:** knowledge_graph-intent turn that runs `search_knowledge_graph` → `explore_entity_neighborhood` (or `find_entity_paths`) → `get_graph_stats`, then synthesizes an entity answer. `extract_entities` is LLM-driven — cover it as a separate near-boundary trial judged, not diffed (or defer it and note, mirroring plan-2's scope discipline).

**Step 1 — neo4j double + seed.** compose adds a `neo4j:5` service on `benchmark-internal` (env `NEO4J_AUTH=neo4j/<benchmark-pw>`, healthcheck `cypher-shell 'RETURN 1'`). Task 1's `[environment.env]` sets `NEO4J_URI=bolt://neo4j:7687` (others keep `bolt://127.0.0.1:1`). The adapter seeds a **~20-entity deterministic graph via direct cypher** through the SAME `knowledge_graph_service` singleton the tools import (landmine 1) or the driver it exposes — every node carries `organization_id = ORG_801`; add 2-3 nodes under `…000810` for the tenant-negative probe. Record the seed as `truth.json` (entity/rel counts + names) for the verifier's direct-count cross-check.

**Step 2 — adapter** consumes `harbor_common`; `validate_network_boundary(..., extra_probes={"neo4j_reachable_via_internal": <bolt probe>})`. Drive `compile_agent_graph` + `astream`; no HITL (no destructive KG tool). Record tool executions, `intent`/`classifier_source` (assert `knowledge_graph`), and an independent post-run cypher count.

**Step 3 — Layer A verifier:** identity + network triple + neo4j-internal-only probe; each KG tool executed with args within impl caps; **tenant scope**: results contain only ORG_801 entities, the `…000810` entity never appears; **`get_graph_stats` totals == direct cypher count** (landmine 2); DATA loop ceiling not exceeded; no destructive tool / no interrupt. Reflection gate skipped for knowledge_graph intent — don't assert reflection milestones.

**Step 4 — Layer B judge** (`harbor_common.judge`): rubric = "the entity answer is consistent with the seeded graph contents (names/types/relationships actually present); no invented entity or relationship." `trusted_sources` = the seeded entities/relationships from truth.json. Calibration stubs the verdict via `_client_factory` + `_judge_stub_verdict`, honored only when `BENCHMARK_CALIBRATION_FIXTURE` set (plan-2 pattern).

**Step 5 — calibration + trials:** `pass.json`→0; `wrong-cross-tenant.json`→10 (a result leaking the `…000810` entity) OR `wrong-hallucinated-entity.json`→10 (judge). Near-boundary: empty-KG query → tool returns empty lists/zeros, answer says "nothing found" (Layer A + judge). Run both no-Docker.

**Step 6 — specs** (rag label order). **Step 7 — task.toml** (`HARBOR_JUDGE_*` in `[verifier.env]`; `NEO4J_*` in `[environment.env]`; base image digest; timeouts 120/360/1800). **Step 8 — commit** `feat(evals): agent-knowledge-graph-flow-v1 (capability 7)`.

---

### Task 2: `agent-memory-roundtrip-v1` (capability 9)

**Files (`evals/agent-memory-roundtrip-v1/`):** `environment/{Dockerfile,docker-compose.yaml,run_agent.py,proxy/…}`, `instruction.md` (the first user turn), `task.toml`, `.gitignore`, `tests/{verify.py,test.sh,calibration/{pass.json,wrong-<mode>.json}}`, `evals/specs/agent-memory-roundtrip-v1/{…}`. No judge, no mock service.

**UUID block 09xx:** ORG `…000901`, USER `…000902`, WORKSPACE `…000903`, THREAD `…000904`; a SECOND thread `…000905` + SECOND user `…000906` for the no-bleed probe.

**Flow (multi-turn, one thread):**
1. Turn 1 — user states a durable fact; `memory_save_node` persists it (fire-and-forget → **drain**, landmine 4).
2. Turn 2 — user asks something that should recall the fact; assert `memory_retrieval_node` surfaced it and the answer uses it.
3. Turn 3 — user asks to forget it; `forget_memory` fires (DESTRUCTIVE → **one HITL approval**, landmine 5); assert removal.

**Step 1 — store choice:** pin the Postgres store (NOT `ENVIRONMENT=testing`). Decide Cohere-on (pgvector semantic) vs Cohere-off (substring fallback) and record in `env_flags` (landmine 6). Default: **Cohere-off substring fallback** — deterministic, no embedding creds, and it exercises the fallback the design doc calls out. `forget_memory` then matches by substring, not the 0.6 threshold — assert that path.

**Step 2 — adapter:** multi-turn driver — send turn 1, **await the memory-save background task / poll the store** until the key lands, send turn 2, then turn 3 with `Command(resume={"confirmed": True})` on the forget interrupt. Independent store read (`get_memory_store` namespace `("user", USER_902)`) before/after forget. Also read the second-thread/second-user namespace to prove no bleed.

**Step 3 — Layer A verifier:** identity + network triple; turn-1 memory row present AND **`redact_pii`-clean** (assert no raw PII in the stored `query` — landmine: redaction at the boundary, `memory_store.py:132`); turn-2 evidence shows the fact retrieved + used; turn-3 `forget_memory` executed behind an approved interrupt with `deleted >= 1` and the store key gone afterward; **no cross-thread/cross-user bleed** (the `…000905`/`…000906` namespaces never received the memory). `semantic = N/A`.

**Step 4 — near-boundary:** `forget_memory` with a query that matches nothing → `{deleted:0, matches:[]}` empty-success (NOT an error) — the memory fake-success trap.

**Step 5 — calibration:** `pass.json`→0; `wrong-<mode>.json`→10 — pick **wrong-unredacted-memory** (raw PII stored → Layer A redaction gate) OR **wrong-forget-before-approval** (HITL snapshot). Run both no-Docker.

**Step 6 — specs** (direct-project-action label order). **Step 7 — task.toml** (`[verifier.env]` empty — no judge; `[environment.env]` = Azure six + the memory/store flags). **Step 8 — commit** `feat(evals): agent-memory-roundtrip-v1 (capability 9)`.

---

### Task 3: docs + manifest + baseline skeletons

- `AGENT_FLOW_BASELINE.md`: promote cap 7 + cap 9 to full gated-format sections (match cap-14 structure: Preconditions / Objective gates w/ file:line / Coverage note / Status / Semantic). cap 7 Semantic = "entity answer consistent with graph"; cap 9 Semantic = "N/A (deterministic)" with the design-doc reconciliation note. Both `Status: NOT YET GATED — awaits first recorded run`. Other rows byte-untouched.
- New dated `source-manifests/agent-flow-2026-08-08-kg-memory.json` + `baselines/agent-flow-2026-08-08-kg-memory.json` (schema clone; digests via `find … -exec shasum -a 256 … | sort -k2 | shasum -a 256`; recorded_at "", benchmarks []; verifier_calibration 2×{pass,wrong}; digest-reproducibility note per plan-2). README table extended.
- Commit `docs(evals): promote capabilities 7 + 9; manifest/baseline skeletons`.

---

### Task 4: calibration gate (Opus, no Docker)
- Each verify.py: `pass→0`, `wrong-*→10` naming the intended gate, via `BENCHMARK_CALIBRATION_FIXTURE`+`VERIFIER_REPORT_PATH`.
- `harbor_common/selftest.py` → `selftest ok`.
- Adversarial per task: corrupt benchmark_id→10; KG cross-tenant leak→10; KG judge stub `supported:false`→10; KG missing judge (no stub, no env)→2; memory unredacted-PII→10; memory forget-before-approval→10.
- Originals + plan-1/2 tasks byte-untouched; digests reproduce locally; `black`/`isort`/`ruff` clean; no `backend/src` touched.

---

### Task 5: recorded runs (GATED — Docker + neo4j + Azure + judge creds; manual)
Prereqs: Docker; `doctl registry login`; image pulled `--platform linux/amd64`; dev-faithful Azure via Infisical `/do-kb` + `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME=gpt-5.6-luna`; judge creds (`HARBOR_JUDGE_*`, gpt-4.1-class — gpt-5 hits the Responses-API version gate). neo4j container comes up in-compose. Run each task; on green (objective 5/5, KG semantic ≥4/5, memory N/A, 0 infra): populate `benchmarks[]`, `recorded_at`, `repository_revision` = merged develop SHA, flip both to GATED, finalize digests in-container. **Never patch a verifier/judge to pass** — a red verifier is signal.

## Risks & decisions carried
1. neo4j singleton binding (landmine 1) — spike at Task 1 Step 1: confirm the adapter's seed writes are visible to the tools' `knowledge_graph_service` singleton before building the verifier.
2. Memory fire-and-forget drain (landmine 4) — the roundtrip's whole validity depends on it; the adapter polls the store, not a fixed sleep.
3. `forget_memory` HITL (landmine 5) — task 2 has an interrupt; assert the pre-approval snapshot (no deletion before approval).
4. Store-backend flag (landmine 6) — default Cohere-off substring fallback; recorded in env_flags; assert the fallback path in `forget_memory`.
5. `extract_entities` LLM non-determinism — judged not diffed; consider deferring to its own trial (note in Coverage).
6. Semantic-scope discrepancy — design doc (N/A for memory) wins over the roadmap line.

## Execution handoff
1. **Subagent-Driven (same session)** — Sonnet implement, Opus spec+quality per task, Fable whole-branch final before PR.
2. **Parallel Session** — new session in the `feat/tool-coverage-bench-plan3` worktree with `superpowers:executing-plans`.

Follow-on: plan 4 (arXiv / E2B / connectors — tasks 1, 7, 8).

---

## Amendment 2026-10-29 — status after merge

Status record checked against `origin/develop` at `749c4b4cd`. The plan text
above stays as written; this section records what happened afterwards.

**Shipped:**

- Tasks 0–4 merged in #1362 (`00adf1303`, 2026-08-09): both task dirs, specs,
  the dated manifest/baseline skeletons, and promotion of capabilities 7 and 9
  in `evals/AGENT_FLOW_BASELINE.md`.
- #1383 (`9f5971ff6`, 2026-08-10) added the 17-task GPT-5.6 job
  `evals/agent-full-benchmark-v1.json`. In that run, knowledge-graph flow and
  memory roundtrip both scored 0/5. Memory failed because the saved memory
  never became observable within the benchmark window.
- #1386 (`856d3dfb4`, 2026-08-11) and #1387 (`d13d1a9e6`, 2026-08-11) fixed the
  agent and tightened both verifiers. The validation table in #1386 reports
  knowledge-graph flow at 5/5 and memory roundtrip at 5/5. The full 17×5
  aggregate rerun was still pending when #1386 merged.
- #1380 (`592125e63`, 2026-08-19) removed the sentinel-intent routing
  workaround from memory roundtrip. Turn 3 now reaches `forget_memory` through
  production routing, and three `wrong-*` calibration fixtures cover it.
  This change merged after the #1386 runs, so that 5/5 result no longer
  describes the current task tree.

**Still open:**

- **Task 5 was never recorded.** In
  `evals/baselines/agent-flow-2026-08-08-kg-memory.json`, `benchmarks[]` is
  empty and `recorded_at` is `""`. Capabilities 7 and 9 are still
  `NOT YET GATED` in `AGENT_FLOW_BASELINE.md`. The #1386 PR-body results were
  never written into the baseline, so they do not count as a recorded run.
- Memory roundtrip needs a fresh 5-trial run on the post-#1380 tree before it
  can be gated.
- Knowledge-graph flow needs a recorded run whose `repository_revision` matches
  the tree that was run. Later edits under `evals/harbor_common/` or the task
  dir change the digests, so `harness_digest` must be recomputed at record
  time.
- `extract_entities` coverage is still deferred (risk 5). The cap-7 coverage
  note marks it out of scope.

**Assumptions to recheck before Task 5:**

- Both Dockerfiles still use `FROM
  registry.digitalocean.com/ragsystemregistry/backend:3a436b2-r1`. Dev has
  since moved to AWS, so confirm this image can still be pulled at the pinned
  digest before scheduling a run.
- Harbor is still pinned at `0.6.6`, task schema `1.2`, in every task.toml,
  source manifest, and baseline, and the local CLI is `0.6.6`. PyPI's latest
  release is `0.25.0` (2026-10-25). Record Task 5 on 0.6.6 so it matches the
  other recorded baselines. Moving to a newer Harbor is separate work, tracked
  in [the Harbor upgrade plan](2026-10-29-harbor-0-25-upgrade.md).
