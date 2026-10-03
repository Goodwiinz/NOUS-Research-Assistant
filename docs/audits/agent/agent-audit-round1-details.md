# Agent audit round 1 — detailed findings — 2026-08-16
Source: opencode session, 4 parallel explore agents (graph/nodes, tools, subgraphs/execution, API layer)
Scope: backend/src/services/agent/**, backend/src/api/agent/**
Status ledger: ~/.audit-ledgers/rag/agent-audit-round1.md

---

## HIGH

### H1 — `_tool_ingest_arxiv` has no `current_user` auth guard (tools_impl.py:1225-1260)
Every sibling tool opens with `if not db or not current_user: return {"error": "Authentication required"}` (e.g. L1497, L1776, L2154); ingest validates only `paper_ids`/`project_id` shape. When `resolve_tool_user` returns None (empty/malformed `user_id` in configurable, inactive/deleted user, org mismatch), the wrapper (tools.py:266-277) still calls it with `current_user=None` and the full arXiv download + PDF extraction + ingest pipeline runs unauthenticated (worker saturation, shared per-IP rate-gate burn under the 120s SLOW timeout).

Worse: the `elif ingested:` fallback (L1345-1350) fabricates `document_ids` from unpersisted objects via `getattr(doc, "id", None)` — phantom IDs that fail downstream (`add_document_to_project` → "not found") or empty list → `ingestion_failed`. Directly contradicts tools.py:137-139, which documents that implementations "fail closed with 'Authentication required'".

Fix: add the standard guard at function top; remove/repair the getattr fallback.

### H2 — terminal job status permanently lost if Postgres blips at completion (job_store.py:343-345 + agent_execution_service.py:2374-2420, 2781-2800)
`set_job` awaits `record_job_status(..., raise_on_error=True)` (job_store.py:345) BEFORE the L1 (348) and Redis (361) writes when status is terminal + thread-scoped. If that PG write raises, the exception escapes `_run_agent_graph`'s own `except Exception`/`except TimeoutError` handlers (the `_set_job_async` call at 2413/2403/2374 IS the handler body), the background task dies, and Redis/L1 keep `running`. The sweeper (`agent_run_tasks.py:397-417`) only fires after `AGENT_RUN_STALE_AFTER_SECONDS=1800` (config.py:638) and — because the live store also says `running` — overwrites with `"Swept as stale..."`, destroying the real `client_safe_error(e)` message. On the COMPLETED path the assistant row was already persisted (2315), so the chat has the answer while the job poll reports failed.

Fix: wrap the strict projection in try/except (log + fall through to L1/Redis writes, or retry once) — terminal status must always reach L1/Redis even if the projection write fails.

### H3 — degraded "no workspace" path uses raw client-supplied `thread_id` as checkpoint key — cross-tenant checkpoint read/write (streaming.py:1250-1253, 1473, 1494-1501, 1528, 1553)
When `_resolve_thread` returns `None` (thread lookup missed AND caller has no live workspace — reachable by deleting one's only workspace), `request_body.thread_id` is left as the client's raw string: it's only rewritten inside `if thread_obj is not None:` (1250-1253). It then flows unchecked into `initial_state["thread_id"]` (1473), `stream_thread_id`/`config["configurable"]["thread_id"]` (1494-1501), `emitter.start(stream_thread_id)` (1528 — hijacks the thread's active stream pointer), and `_clear_stale_pending_confirmation(graph, config)` (1553 — can destroy the victim's pending HITL interrupt). `astream_events` with a checkpointer merges input into the existing checkpoint: an attacker sending another user's thread UUID gets the victim's full conversation history into the model context and the answer streamed back as `token` frames; the attacker's turn is then appended into the victim's checkpoint (memory_save reducer), and the checkpoint's `user_id` channel is overwritten with the attacker's id — locking the victim out of their own `/stream/confirm` (ownership check at streaming.py:2367-2368 then fails). Contrast: `/execute` sanitizes this exact case (execute.py:465 nulls `thread_id` when resolution misses).

Fix: mirror execute.py — null the thread_id when `_resolve_thread` returns None and a client thread_id was supplied but not owned.

---

## MEDIUM

### M1 — `tool_executions` never reset per turn (_nodes_classify.py:220-240, with _nodes_memory.py:265-270, 299)
`preprocessing_node` resets `plan`, `reflection_count`, `tool_loop_count`, `error_count`, `user_confirmed`, `compaction_count`, etc., but NOT `tool_executions`. It persists in the checkpoint across turns (only pruned to last 20). `memory_save_node`'s save gate (`intent in ("", "general") and not tool_executions`) therefore sees prior turns' executions: after ONE tool turn in a thread, every later general turn — including "hi"/"thanks" — passes the gate and is persisted to long-term memory (the noise-recall bug the gate was built to prevent, trace 019e066b). Also `tools_used: tool_executions[-3:]` attributes prior-turn tools to the current turn's memory. `tool_dedupe.py` correctly scopes executions to the current turn — the memory gate does not.

Fix: reset `tool_executions` in `preprocessing_node` alongside the other per-turn resets (keep pruning as backstop for old checkpoints).

### M2 — ownership-check failure widens retrieval scope to org-wide (_nodes_rag.py:187-189, 399-411)
`_user_owns_project` returns `False` on any exception ("fail closed"). But the caller treats False as "not owned" → `scoped_project_id = None` → `_try_primary_do_kb_read_impl` proceeds with an org-wide DO KB read, injecting sibling projects' chunks into the prompt. A transient DB outage during the check therefore WIDENS the retrieval scope from one project to the whole org — fail-open in effect despite the fail-closed comment. Concrete: DB blip at line 185 → cross-project document content surfaces in the answer context.

Fix: distinguish exception (abort project-scoped read) from verified-not-owned.

### M3 — bare-UUID fallback misclassifies any pasted UUID as `project_id` (graph.py:36-37 + _nodes_rag.py:694-710, 715-740)
`_extract_project_id_from_text` falls back to ANY UUID in the message text. A user pasting a document UUID, message id, or run id (all UUIDs in this system) gets it stored as `current_project_id`, and `page_context` is force-set to `type="project"` (rag_node 705-709 / 734-740) with no ownership verification on this propagation path. Consequences: `_build_page_context_line` tells the LLM "use this project_id, do NOT ask"; `_with_injected_project_id` (_nodes_tools.py:383-399) auto-injects the bogus id into tool args; `_user_owns_project` then fails → DO KB scope silently dropped to org-wide or hybrid returns [] (silently empty retrieval). Mitigated only by tool-layer ownership checks; the turn is still misdirected.

Fix: verify ownership before promoting extracted UUID to current_project_id, or drop the bare-UUID fallback.

### M4 — stale `last_user_msg` for multimodal turns → canned greeting answers the wrong message (_nodes_llm.py:264-271, 277-280)
`last_user_msg` walks `reversed` filtered on `isinstance(m.content, str)`. For an image-only (or list-content) HumanMessage it picks the previous turn's text. If that stale text is a greeting ("hi"), the greeting fast-path fires: `retrieved` is `[]` for image-only turns, so the node returns a templated "Hi again…" AIMessage and never sends the image to the model. Same stale string feeds `_tools_for_turn` (353). Contrast `_classify_core`/`rag_node`, which coerce via `_coerce_text`.

Fix: use `_coerce_text` when selecting last_user_msg; skip greeting fast-path when content is non-string/list.

### M5 — multimodal content raises in `is_conversational`, memory recall silently dropped (_nodes_memory.py:166-179)
Line 168 assigns raw `msg.content` (no `_coerce_text`, unlike sibling nodes). For list content, `is_conversational` (→ `_nodes_rag.py:201` `content.strip()`) raises AttributeError. The call at 179 sits OUTSIDE the node's `try` (starts line 182), so the exception propagates to `preprocessing_node`'s `gather(return_exceptions=True)`, which swallows it into default `{"user_memories": []}` + one WARNING. Every multimodal turn silently loses long-term memory recall.

Fix: coerce text; move the call inside the try.

### M6 — `return_exceptions=True` turns total connector failure into a success-shaped empty result (tools_impl.py:3023-3056)
Failed connectors are logged and skipped (3030-3036); if every connector errors, the payload returns `total_results: 0` with no `"error"` key. Failure chain: `_nodes_tools.py:492` keys error classification off `"error" in result` → execution recorded `status="completed"` → `tool_dedupe.find_cached_tool_results` treats it as a good result (legitimate same-turn retry short-circuited with the empty payload), and `find_repeated_failures` never sees it. Net: "search returned nothing" reported to the user when the truth is every connector 500'd/timed out, and the agent is prevented from retrying.

Fix: if all connectors fail (and ≥1 was attempted), return `{"error": ...}`.

### M7 — invalid connector name silently degrades to fan-out across ALL connectors (tools.py:771-776 + tools_impl.py:2992-3018)
`_validate_connector_name` returns `None` on regex mismatch, the arg is dropped, and the impl takes the `list_available()` branch — every registered connector (~250) searched concurrently. The impl has a proper "Unknown connector" error (2993-2999) but it's unreachable for typo'd/malformed names (e.g. `pubmed!`). Combined with 30s timeout + 2 outer attempts, one hallucinated name yields two ~250-connector HTTP bursts near-always ending in TimeoutError, plus third-party rate-limit exposure.

Fix: `_validate_connector_name` should reject (return error) rather than drop invalid names.

### M8 — silent truncation of `paper_ids` to 10 loses papers under success status (tools.py:270)
`capped_ids = list(paper_ids or [])[:_MAX_INGEST_BATCH]` drops IDs 11+ without signal; `_tool_ingest_arxiv` deliberately returns `{"error": "Maximum 10 papers per ingest request"}` (tools_impl.py:1238-1239) for exactly this case, but the wrapper prevents that error from ever firing via the LangGraph path. Concrete: model ingests 15 IDs → 10 land → result says "Ingested 10 of 10 paper(s)", status `ingestion_complete` — 5 papers silently vanish with no error anywhere.

Fix: remove the wrapper cap, let the impl's explicit error reach the model.

### M9 — `_resume_agent_graph` pins an asyncpg connection across the up-to-360s graph run (agent_execution_service.py:2442, 2558-2563, 2591-2595)
`get_run(db, ...)` at 2558 is a bare SELECT: SQLAlchemy autobegins a transaction and holds the checked-out connection until commit/rollback/close. No db op commits between 2558 and `graph.ainvoke` at 2591-2595, so every in-flight confirm holds one pool connection for up to 6 minutes → pool exhaustion blocks all requests under modest concurrency. Conditional variant in `_run_agent_graph`: reads at 2124-2145 open a txn, and when the skill flags are off `create_runtime_snapshot` returns without touching the session (runtime_snapshot.py:220-224), leaving the txn open through the ainvoke at 2242-2243.

Fix: commit/rollback (or use a short-lived session) before ainvoke.

### M10 — `set_job` monotonic guard compares a stale `existing` captured before an await (job_store.py:311-328 vs 348-361)
`existing` is snapshotted under the first lock (311-328), then a full PG round-trip awaits (343-345, widening the race window to seconds), then the second lock block (348-353) still evaluates `_is_newer_or_equal(data, existing)` against the pre-await snapshot. A newer L1 write landing during the await (e.g. `get_job_fresh` folding a fresh Redis record at 573-577, or another writer's `set_job`) is unconditionally stomped, and `set_job_redis_only` (361 → 364-384) has no guard, so Redis is stomped too — an older status can resurrect over a newer one cross-worker.

Fix: re-read `_l1.get(job_id)` inside the second lock block.

### M11 — circuit-breaker exit and HITL-deny leave dangling `tool_calls`; stale answer persisted (subgraphs/_factory.py:182-183, + _builders.py:80-86, _factory.py:390-401)
When `error_count >= 3` trips while the last AIMessage still carries `tool_calls` (3 failing tool batches while the model keeps re-calling), `should_continue` routes straight to reflection → END, persisting an AIMessage with unanswered tool_calls. Same on HITL deny (390-398): a new AIMessage is appended without answering the pending calls. `_sanitize_messages` repairs the next turn's prompt, but in THIS turn `_run_agent_graph`'s extraction (2280-2283) walks past the content-less tool_calls AIMessage to the previous turn's final AIMessage and persists that stale content as the new assistant row + COMPLETED job.

Fix: append placeholder ToolMessages on breaker-exit and deny paths (like sanitizer does).

### M12 — `get_thread_messages` count query omits the `before` cursor filter (execute.py:1321-1330, page query 1307-1317)
Page query filters `thread_id + superseded_by IS NULL + created_at < before`; the `total` count filters only `thread_id + superseded_by IS NULL`. Any paged call with `before=` returns `total` = full-thread count instead of the filtered count, so clients computing remaining/older-message counts from `total - received` drift. Exactly the documented "count must apply the same filters as the result query" bug class. `has_more` unaffected (sentinel-based).

### M13 — `replay_buffered_stream` emits no heartbeat frames during silent gaps (streaming.py:922-969)
Live-stream heartbeats are emitted with `buffer=False` (main path 1684-1688, fast path 427-435), so they never enter the resumable buffer. A client attached via `GET /stream/resume` following an active run receives zero bytes whenever the producer is in a long silent phase (planner/classifier/reflection — the ~20s+ gaps the keepalive exists for, comment at 972-974). Proxies with ~30s idle timeouts kill the replayed connection mid-run; client reconnects and replays into the same silent gap, repeating. Replay loop polls every 1s for up to 600 iterations but never yields a keepalive frame — keepalive parity broken between live and resume paths.

Fix: emit non-buffered heartbeat frames from the replay loop on poll ticks with no new data.

---

## LOW

### L1 — sanitizer leaves tool_call with empty/missing id unanswered (graph.py:100-103, 244-246, 315)
`_tool_call_id` returns `None` for a missing/empty id → `continue` → no placeholder ToolMessage emitted, but the AIMessage retains the tool_call → OpenAI contract still violated → 400 on next `llm_node` invoke, retried 3× by `_RETRY_POLICY`, then turn fails. Same skip (102, `tc_id in placed_tm_ids`) leaves the second AIMessage unanswered if two AI messages share a tool_call_id (checkpoint replay duplication). The repair path exists for missing ToolMessages but not for unidentifiable tool_calls — the tool_call itself should be stripped.

### L2 — HITL audit-row failure swallowed at DEBUG (_nodes_tools.py:161-174)
`_write_hitl_audit_row`'s `except Exception: logger.debug(...)`. A compliance-critical "who approved the destructive action" record can be silently missing with no visible log at default level. The structlog `hitl_decision` event survives, but the durable row — whose stated purpose is outliving log retention — fails invisibly. Raise to ERROR.

### L3 — LLM stream iterator never closed on early exit (fast_path.py:144-168)
`stream_fast_path_chunks` `finally` cancels `first_task` and awaits `persist_task`, but never closes `iterator` (`llm.astream(...)`). When the caller closes this generator mid-stream (client disconnect — `cancel_fast_path` in streaming.py:361-384 returns from inside the `async for`), the underlying astream async generator is abandoned suspended, holding its HTTP response until GC-driven asyncgen finalization (nondeterministic). Repeated mid-stream disconnects accumulate held connections. Also 165-168: `contextlib.suppress(Exception)` around `await persist_task` hides a failed durable user-message persist with no log.

### L4 — `_LLM_CACHE` key omits credentials (graph.py:244-246, 315)
Cache key is `(endpoint_type, deployment)` only. Rotating `AZURE_OPENAI_CHAT_API_KEY`/endpoint in settings leaves stale cached clients authenticating with the dead key until process restart — every turn fails auth (2 client retries each) after a credential rotation deploy that doesn't restart pods (e.g. Infisical secret sync without rollout). Include a credential hash/fingerprint in the key.

### L5 — `failed_papers` entries never cleared when the stub path later ingests successfully (tools_impl.py:1272-1275, 1284-1288)
A transient batch-metadata failure marks every requested ID `"metadata fetch failed"`; `_paper_or_stub` then ingests stubs which can succeed, but the per-paper failure dict is never reconciled against what actually landed. Result: `status=ingestion_complete` with `failed_papers` listing every paper (detail string only appended when count==0, 1448-1454). Model receives a self-contradictory payload, typically reports partial failure and re-ingests (idempotent via reuse, but a false failure report to the user). Fix: drop failed_papers entries for IDs that landed.

### L6 — `list_project_documents` bypasses the required status mapping (tools_impl.py:2010-2012)
Returns raw `d.processing_status.value` (e.g. `"completed"`), while `search_documents` (1530-1534) correctly maps via `ApiDocumentStatus.from_db` → `"indexed"`. Same document reports different statuses depending on tool — inconsistent availability signal to the LLM.

### L7 — compare_documents cap mismatch: wrapper 10, impl 5 (tools.py:80,528 vs tools_impl.py:2299-2300)
`_MAX_COMPARE_DOCUMENTS = 10` lets 6-10 document calls through the wrapper; the impl then hard-errors `"Maximum 5 documents can be compared at once"`. The wrapper cap is dead, contradictory metadata, guarantees an error loop for legitimate 6+ comparisons instead of a clean cap. Align (preferably raise impl or lower wrapper).

### L8 — `export_bibliography` never caps `document_ids` (tools.py:676-692)
Only ingest (10), compare (5/10), graph limits are capped; this tool forwards an arbitrary LLM-supplied list into `Document.id.in_(valid_uuids)` + `Citation.document_id.in_(owned_ids)` (tools_impl.py:2853, 2871). A hallucinated list of thousands of UUIDs produces an unbounded IN-clause pair per call. Cap it.

### L9 — `execute_code` falls back to `thread_id="default"` for a stateful sandbox (tools.py:723)
E2B sandboxes are keyed by thread_id and persist variables/files across calls ("stateful within a conversation"). If `configurable.thread_id` is missing/empty (misrouted invoke, synthetic runner, test), distinct users land on the same `"default"` sandbox — cross-conversation code/state/file leakage. Fail closed instead of sharing one named sandbox.

### L10 — `search` interpolated into `ilike` without `_escape_like` (project_service.py:72, agent path tools_impl.py:2050-2060)
Every agent-side query escapes LIKE metacharacters (tool_helpers.py:25-27); the list_projects search filter doesn't, so an LLM-supplied `search` containing `%`/`_` matches unintended project names. Parameterized (not injection), pattern only broadens, but breaks the codebase's wildcard-escaping invariant and can surface wrong projects to a write-flow that then asks which one to target.

### L11 — `_set_job` bypasses the L1 monotonic guard and never stamps `_seq` (agent_execution_service.py:119-156)
Line 144 `_jobs[job_id] = data` is unconditional (no `_is_newer_or_equal` check) and the payload lacks `_seq`, so a `_set_job` write (execute.py:384/401/533) can overwrite a newer L1 status — precisely the delayed-write stomp the guard in `job_store.set_job` exists to prevent. Exposure low (creation-time callers).

### L12 — failed Redis init leaks the client (job_store.py:129-135)
On ping failure, `_redis = None` but the client created by `from_url` (130) is never `aclose()`d. Since `_redis is None` makes every caller retry init, a Redis outage leaks one connection-pool object (fds/sockets) per retry cycle for the outage duration.

### L13 — memory namespace has no `organization_id` (memory.py:168, 194, 266)
`search_memories`/`save_memory`/`delete_memory_by_query` all use `namespace = ("user", user_id)`; org is never part of the key despite the mandatory tenant-scope rule and despite the durable `agent_memories` table being org-scoped (`organization_id` NOT NULL, memory_store.py:37). Memories are user-scoped only; leak requires one identity spanning orgs, hence low.

### L14 — non-atomic append; orphaned no-TTL key possible (stream_buffer.py:64-68)
`rpush` → `ltrim` → `expire` are three separate round-trips. A connection drop between the first `rpush` (which creates the key without TTL) and its `expire` — when no further append follows — leaves an `agent:stream:{id}` list that never expires. Related: trimming at 5000 gives a reconnecting consumer no gap signal, so frames silently missing from a resume are undetectable (`read_after` just returns whatever survived). Use a pipeline/Lua script; emit a gap/sequence marker on trim.

### L15 — `read_after` materializes and JSON-parses the entire buffer per poll (stream_buffer.py:76-78)
`lrange(key, 0, -1)` + parse of up to 5000 frames on every poll tick per following client — O(n) CPU/allocs per poll that grows with run length; a busy stream multiplied across reconnecting clients burns event-loop CPU. Could be `seq`-indexed (zrangebyscore or per-frame keys).

---

## Verified NOT buggy (checked, no finding)

- **Checkpointer URL** — checkpointer.py:39-42 rewrites `postgresql+asyncpg://` → `postgresql://`; correct.
- **Recursion/loop limits** — RECURSION_LIMIT=50 wired at every invoke site (streaming.py:1496/2575, agent_execution_service.py:2207/2464, synthetic_traffic.py:571); research 5 / writing 8 / data 8 / main 6, + force_synthesis bump + `_force_synthesis_fired`; reset per turn at _nodes_classify.py:239. No infinite routing loop.
- **Tenant scope on tool DB queries** — every document/citation/KG query filters `organization_id`.
- **Project ownership in tools** — `_verify_project_ownership` (Workspace.owner_id join) on every project-accepting tool incl. `do_kb_retrieve`.
- **HITL gating** — destructive tools all DESTRUCTIVE-tagged, gated on main graph (`has_policy`) and subgraphs (`has_policy_in_subgraph`).
- **arXiv document_ids** — ingest returns/uses persisted UUIDs (except H1 phantom path).
- **SQL injection** — parameterized throughout, no LLM-controlled sort strings.
- **tool_session lifecycle** — rollback on BaseException incl. cancellation, commit-before-pool-release, per-call sessions.
- **tool_dedupe keying** — no exploitable collision; project-injected args keyed consistently; failure-status entries excluded from completion cache.
- **Thread/job/confirm ownership (API)** — fail-closed checks everywhere except H3 degraded path.
- **Confirm double-resume** — PG atomic claim + Redis CAS + checkpoint interrupt guard + endpoint ownership check.
- **run_event_store** — seq/terminal races handled; payload schemas match every finalize payload.
- **Thread summarization** — Celery-enqueued, no sync-service-on-AsyncSession misuse.
- **SSE live path** — frame format, disconnect detection, shielded cleanup, terminal-event handling correct (M13 is resume-path only).
- **LangSmith synthetic filtering** — metadata.synthetic + run_name, no post-hoc tag updates.
- **Mermaid/visualization** — inputs server-controlled enums, escaped.
- **Compactor** — RemoveMessage + same-id replacement, correct add_messages pattern, no orphaned ToolMessages; checkpoint pruning respects HumanMessage boundaries.
- **Off-by-one** — checkpoint window exactly 20 turns; `[-20:]`, `[:3000]`, `[:512]`, `[:5]` all correct; redaction before slicing.
- **Query-param shadowing** — none in agent endpoints.
- **DraftGenerationService** — background path uses fresh AsyncSessionLocal.
- **FileService sync extraction** — no db use in thread.
