# Agent audit round 8 — fix plan

Date: 2026-10-01
Audit SHA: `1092b380bd657cb7bb4c3a2dbf8795a1b0eef153` (tip of `develop`, #1783 merged)
Linear epic: GOO-361 (children GOO-362+ per finding).
Ledger: `~/.audit-ledgers/rag/agent-audit-round8.md` (+ `-details.md`) — status source of truth; claim by ID before touching a slice.
Lens: agent architecture (graph wiring, state, durability, SSE) + bug hunt. Prompt-injection was round 7 (#1594–#1597) and is out of scope here.

## Result

32 findings: 2 high, 12 medium, 18 low. Nothing crosses a tenant boundary. The two highs are silent functional regressions in production:

- **R8-A1** — `is_conversational` word-matches "no", "good", "yes", "what model" anywhere in the message, so substantive questions skip RAG and memory recall.
- **R8-B1** — `do_kb_retrieve` has returned an empty `invalid_document_scope` result for every unscoped call since #1716 (2026-09-28): schema validation injects `document_ids: None`, and the impl tests `"document_ids" in args`.

### Cross-cutting themes

1. **Per-turn state reset clobbers checkpoint carry-over.** Both `initial_state` builders and `runtime_state_fields` seed last-value channels every turn (A3 `turn_index`, A6 `current_project_id`). One canonical per-turn-reset function is the structural fix.
2. **#1716 changed tool-arg shape with no `execute_tool`-level tests.** Validation adds defaults and drops unknown keys; `tools.py` wrapper bodies are dead (only signatures matter). B1, B2 and every wrapper-body guard (`_clamp_int`, `_reject_over_cap`, allowlists) fall out of this.
3. **DB sessions pinned across SSE streams.** Stop-marker SELECT twice per graph event on an uncommitted session (D1), request-scoped session held through replay (D2), 2 Hz poll whose single failure cancels the graph (C1). No per-user concurrent-stream cap (R7-M6 residual) means one account can exhaust the 15+5 pool.
4. **Run status has three writers.** Submission service (with ledger events), `upsert_run` via job_store + sweeper (no events), confirmation claim/release (no events). Ledger, status and outbox drift (C4, C5).
5. **`api/agent` is outside the architecture gates.** `tests/unit/architecture/*` scans only `api/threads/workspace_routes`; `execute.py` commits, opens sessions, and hand-rolls ownership joins (D3).

## Slices

Each slice = one branch `fix/agent-r8-<slice>` cut from `origin/develop`, one PR, Opus implementer at xhigh effort. Claim the IDs in the ledger before branching. Every fix lands with the mutation test from the details file (test must fail with the guard removed — `docs/engineering/testing.md`).

| # | Branch | IDs | Files | Depends on |
|---|--------|-----|-------|------------|
| S1 | `fix/agent-r8-tool-args` | **B1**, B2, B4, B6 | `tools_impl.py`, `_nodes_tools.py`, `tools.py` | — |
| S2 | `fix/agent-r8-rag-gating` | **A1**, A4, A10 | `_nodes_rag.py`, `_nodes_memory.py`, `fast_path.py` | — |
| S3 | `fix/agent-r8-turn-state` | A3, A6 | `agent_execution_service.py:2570`, `api/agent/streaming.py:2284`, `runtime_snapshot.py`, `_nodes_rag.py` promotion | — (touches S6/S8 files; merge first) |
| S4 | `fix/agent-r8-reflection-planner` | A2, A5, A7, A8, A9 | `reflection.py`, `planner.py`, `_builders.py`, `_nodes_llm.py`, `subgraphs/_factory.py`, `capability_terminal.py` | — |
| S5 | `fix/agent-r8-collection-links` | B3, B5 | `tool_helpers.py`, `tool_operations.py`, `tools_impl.py` ingest attach | S1 (tools_impl.py) |
| S6 | `fix/agent-r8-sse-sessions` | D1, D2, D5, D8, C5 | `api/agent/streaming.py`, `api/agent/execute.py` resume, `agent_run_service.py` | S3 |
| S7 | `fix/agent-r8-agent-read-routes` | D3, D4, D6, D7 | `api/agent/execute.py`, `schemas.py` | S6 (execute.py) |
| S8 | `fix/agent-r8-run-lifecycle` | C1, C2, C3, C4, C6 | `agent_execution_service.py`, `agent_submission_service.py`, `agent_run_service.py`, `tasks/agent_run_tasks.py` | S3 |
| S9 | `fix/agent-r8-observability` | C7, C8 | `iteration_ledger.py`, `observability.py`, Celery worker init | — |

Parallel waves: **wave 1** S1, S2, S3, S4, S9 (disjoint files). **Wave 2** S5, S6, S8 after S1/S3 merge. **Wave 3** S7 after S6.

## Per-slice contract

### S1 — tool args (B1 high, B2, B4, B6)
- B1: in `_validated_tool_arguments` dump with `exclude_unset=True` so LLM-omitted optionals do not appear; keep the mutation `arguments_hash` on the full canonical args (dedupe key stability). Alternative if the hash contract makes `exclude_unset` awkward: impl switches to `args.get("document_ids") is not None`. Do both if cheap.
- B2: forward page-context `project_id` as server-owned scope — `_dispatch_tool` passes it as a kwarg into `_tool_do_kb_retrieve` (not through the LLM schema).
- B4: when `truncated` is set, rewrite `returned`/`has_more` from retained identities, or clamp `limit` so a page fits the 32 KiB cap.
- B6: catch pydantic `ValidationError` in the mutation branch separately → `invalid_tool_arguments`, `automatic_retry_allowed=True`.
- **Class test (required):** parametrised test that drives every registered tool through `execute_tool` with LLM-minimal args (required fields only) and asserts the impl receives no injected `None` optionals and no dropped server-injected scope. This is the regression gate for theme 2.
- Decide and record: delete `tools.py` wrapper bodies (dead) or add the guard-diff test. Also delete stale `AGENT_TOOLS` in `tools_impl.py:151-718` if its only consumer is the `execute.py` re-export (verify).

### S2 — RAG gating (A1 high, A4, A10)
- A1: `is_conversational` must fullmatch the normalized message (reuse `_BARE_CONVERSATION_RE` / `is_bare_greeting_text` semantics) or require every token conversational and token count < N. Shared predicate — both `rag_node` and `memory_retrieval_node` change together.
- A4: fast path handles greetings only; acks ("ok", "great", "thanks") are ineligible when the previous assistant message ends in a question/proposal or when a project page context is present. Fast path is on in values-dev/values-aws, so this is live.
- A10: short-circuit conversational turns before the thread-attachment branch in `rag_node` (explicit `attachment_ids` still win).
- Tests: the mutation checks in the details file (A1 two asserts; A4 `classify_fast_path_turn` ineligible; A10 `_load_attachment_contexts` not awaited).

### S3 — turn state (A3, A6)
- A3: drop `"turn_index": 0` from both `initial_state` dicts; checkpoint carries it. Test with a compiled graph + MemorySaver, real `initial_state` shape, two turns on one `thread_id` → `turn_index == 2`. Existing tests call the node directly and mask this — keep them, add the graph-level one.
- A6: either persist the verified id into the thread binding when `rag_node` promotes it, or stop seeding `current_project_id` when the request has none. Ownership gate already applies at promotion. Prefer the thread-binding write (durable, visible to `_resolve_and_bind_project`).
- Architecture: introduce one `reset_turn_state(...)` helper used by both builders; the four reset owners (two builders, `preprocessing_node`, `_clear_stale_pending_confirmation`) must route through it or be documented as deliberate exceptions.

### S4 — reflection/planner (A2, A5, A7, A8, A9)
- A2: on `revise`, executor nodes (`llm_node` + three subgraph variants via `_factory`) append a transient, bounded, sanitized SystemMessage built from `_reflection_result.issues`. Do not persist it. Note the streaming side already emitted the first answer — coordinate wording with D-side observation (second visible answer) but no streaming change in this slice.
- A5: `validate_plan` treats names unknown to the registry (and `"N/A"`) as malformed → continue without a plan. Terminal `capability_limitation` only for registered tools unavailable in this branch or explicit `outcome="unsupported"`. Update `test_task3_runtime_projection.py:500-514`, which pins the old behaviour.
- A7: `_prior_turn_read_happened` scans only messages before the latest `HumanMessage`.
- A8: route a fully-deduped batch with a reason flag (`dedupe` vs `ceiling`); forced synthesis guidance and `record_loop_exhaustion` key off it.
- A9: `capability_terminal` accepts `"completed"`; fix the fixture in `test_task3_capability_terminal.py` to the real status string.

### S5 — collection links (B3, B5)
- B3: `_link_documents_to_project` uses `on_conflict_do_update(set_={"is_deleted": False, "deleted_at": None})` and counts revived rows as linked. Check `collection_service.py:258-278` REST re-add (R6-M7) — fix there too if it shares the helper, otherwise leave a ledger note.
- B5: replayed external-barrier results carry `replayed_from_operation`, set `automatic_retry_allowed=False`, and count toward `error_count`.

### S6 — SSE sessions (D1, D2, D5, D8, C5)
- D1: time-gate the stop poll to ≤1 per `_SSE_DISCONNECT_POLL_SECONDS`; each poll in a short-lived `AsyncSessionLocal()` (or `await db.rollback()` after) so no transaction spans graph awaits. Apply to `/stream`, `/stream/confirm`, Luna.
- D2: `await db.rollback()` before every `StreamingResponse` in `resume_stream` and before the replay loop at `streaming.py:2079` (codex branch at `:1992-1995` is the model).
- D5: define `durable_stop_requested` / `cancel_current_stream` (or no-op stubs) before the `try`.
- D8: finalize FAILED (in try) before yielding ERROR on `/stream` and Luna, mirroring the done path.
- C5: wrap the graph phase of `/stream` in `_run_heartbeat(acceptance.run_id)`.
- Also fix the drifted confirm-path FAILED finalize (`:4502-4512`) to pass `error_code`/`error` like `:3297-3309`.

### S7 — agent read routes (D3, D4, D6, D7)
- D3: message reads and trace resolve via `workspace_access.get_thread(db, id, current_user.id)`; the list adds `Conversation.is_deleted == False`, `Workspace.is_deleted == False` and a membership predicate. Add an architecture test that scans `api/agent` for the same rules as `workspace_routes` (no commits, access through `workspace_access`). Expect it to flag `execute.py:1014,1055` commits — fix or allowlist with a dated reason.
- D4: `PageContextRequest` gets `max_length` on string fields and a serialized-size validator on `metadata` (≈8 KB); job payload stores `_page_context_to_dict(...)`, not the raw model. OpenAPI snapshot + `api.d.ts` regenerate in the same PR (`generate_openapi.py`, `generate:api-types`).
- D6: `_pending_confirmation_frame` returns a frame only when `active_run.status == AWAITING_CONFIRMATION` (legacy no-run path unchanged).
- D7: `redact_tool_executions` on `tool_executions` and `result.tool_executions` in `get_job_status`.

### S8 — run lifecycle (C1, C2, C3, C4, C6)
- C1: `monitor()` logs and continues on non-`CancelledError`; fails only after N consecutive errors. Keep strict pre-start and post-return checks.
- C2: **decision required before coding** — either `ActiveRunConflict` when `active.user_id != submitter`, or editors may supersede (document in `docs/engineering/backend.md`, record actor in `run.cancelled` payload). Default to the conflict unless the user says otherwise; it is the safer change.
- C3: on a retry where the terminal UPDATE matches no row, re-read status + terminal event; return True if they equal the requested transition.
- C4: sweeper appends the matching terminal event (absorb `RunAlreadyTerminalError`) and marks pending outbox rows FAILED in the same transaction.
- C6: narrow the `except Exception` at `agent_execution_service.py:2456` to the persist call; `_resolve_thread` failures fail the run.

### S9 — observability (C7, C8)
- C7: `redact_nested_pii` over `state_snapshot` and `config` before writing in `iteration_ledger.py`.
- C8: `configure_langsmith()` in the Celery `worker_process_init` hook, and pass `hide_inputs`/`hide_outputs` explicitly when the client is built so a cached env read cannot defeat it.

## Verification per PR

```sh
scripts/ci/run_local_ci.sh --base origin/develop          # all blocking gates
pytest -q backend/tests/unit/architecture backend/tests/unit/api
pytest -q backend/tests/services/agent backend/tests/unit/agent backend/tests/unit/services  # agent suites
python scripts/ci/generate_openapi.py --check               # S7 only (schema change)
```

Mutation verification is mandatory for every race/idempotency-shaped fix (C1, C3, C4, D1, D2, B3, B5): revert the guard locally, run the new test, confirm red, restore.

## Deferred (tracked in ledger log, not in a slice)

- Per-user concurrent-stream cap (R7-M6 residual) — bounds D1/D2 blast radius; design call.
- Shared-thread semantics (one checkpoint + one writer slot per membership-based thread) — C2 is a symptom; needs a product decision.
- Move `streaming.py` orchestration into `services/` so import-direction gates see it.
- Dedupe key computed on raw args vs execution on validated args — harmless now, drift-prone.
- `filtered_tool_node` vs `tool_node` divergence (`loaded_skill_versions`, `last_error_info`) — extract shared function.
- Recursion headroom (writing/data subgraph ≈40 steps vs `RECURSION_LIMIT=50`).
- Compactor's dependence on `add_messages` un-marking `RemoveMessage` (langgraph pin `>=0.4,<2.0` too loose).
- `langgraph` psycopg pool has no checkout `check`; ~33 connections/pod vs RDS limits.
- Stray remote ref `refs/remotes/origin/develop` (140 commits stale) makes `launch_audit.sh --ref origin/develop` pin the wrong SHA. Use `--ref develop`; delete the ref.
