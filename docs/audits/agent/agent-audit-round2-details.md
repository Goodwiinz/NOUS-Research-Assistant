# Agent audit round 2 — detailed findings — 2026-08-17
Source: opencode session, 4 parallel explore agents (arxiv/draft/project, file/summarize, Celery workers, RAG retrieval core)
Status ledger: ~/.audit-ledgers/rag/agent-audit-round2.md

---

## HIGH

### R2-H1 — tracker KG integration omits organization_id → cross-tenant KG mixing (arxiv_change_tracker.py:511, :577)
`_ingest_new_paper` and `_update_knowledge_graph` both hold `organization_id` but call `kg.process_paper_kg_integration(paper)` with no org. Signature default is `None` → `_add_entity_to_kg`/`_add_relationship_to_kg` pass `organization_id=None` → `create_entity` MERGEs with `org_key = ""` (knowledge_graph_service.py:538). Every tracker-driven ingest writes entities/relationships into the shared "no-org" partition; two orgs tracking the same category MERGE onto the same nodes — tenant mixing in KG. The plumbing exists (`process_paper_kg_integration(paper, organization_id=...)`) and is simply not used.

### R2-H2 — delete_project hard-DELETE hits RESTRICT FKs → 500 (project_service.py:228-234)
`await self.db.delete(project)` issues real `DELETE FROM collections`. `project_skills.project_id` → `ondelete="RESTRICT"` (project_skill.py:40), `agent_runtime_snapshots.project_id` → `ondelete="RESTRICT"` (agent_runtime_snapshot.py:23), and `Collection` has no relationship to either → ORM cannot cascade. Deleting any project that has a skill or runtime snapshot → FK violation → IntegrityError → unhandled 500. Also contradicts the codebase's soft-delete convention (`is_deleted` filters everywhere else; list endpoint comment at :55-58 exists precisely because deleted rows persist).

### R2-H3 — abstract changes never detected → permanent missed updates (arxiv_change_tracker.py:246-259 + :536-544)
`fields_changed` is built only from `title`, `authors`, `primary_category` (:250-259). `_update_existing_paper` gates the content write on `{"title", "abstract"}.intersection(changed_fields)` (:538-544) — `"abstract"` can never be in that set. Paper v2 with changed abstract only → hash differs → "updated" ChangeRecord with `fields_changed=[]` → `text_changed=False` → `content_text` and search vector NOT updated, but the new hash IS persisted to state (:276). The change is never re-emitted — DB keeps the old abstract forever.

### R2-H4 — FileService() built without required db arg in DO KB pre-flight → forced extraction dead (pre_flight.py:75)
`FileService.__init__(self, db: AsyncSession)` (file_service.py:69) takes a required positional. `FileService()` at pre_flight.py:75 raises TypeError on every call where `should_force_text_extraction(document)` is true; the broad except (:93) swallows it and returns False. Large-PDF forced text extraction NEVER runs — every flagged doc silently falls through to the raw-PDF KB path this guard exists to avoid. Log says "text extraction failed" but cause is a constructor bug.

### R2-H5 — fail_job before self.retry: retry machinery self-destructs (document_processing_tasks.py:127-153)
Except path does `job.fail_job(str(e)); db.commit()` → status FAILED (terminal), then `raise self.retry(...)`. On the retry delivery, `claim_job_for_processing` (:60) sees FAILED ∈ terminal → `ClaimResult(False, "terminal")` → task skips. `max_retries=3` + exponential backoff can never re-process; any transient error (storage blip, connection drop) permanently fails the upload. Only manual `retry_failed_processing` (itself dead code, no dispatcher) could recover.

### R2-H6 — no rollback before fail_job in except (document_processing_tasks.py:127-133)
If the original exception poisoned the session (any DB error), `job.fail_job(...)` + `db.commit()` raises PendingRollbackError, which propagates INSTEAD of the `self.retry` call → no retry, no FAILED status, job + document stuck RUNNING/PROCESSING until 30-min stuck-sweeper. Sister tasks in processing_tasks.py:460-486 fixed this exact pattern (rollback first); this file didn't get the fix. Stacks with R2-H5.

### R2-H7 — document never leaves PROCESSING on task failure (document_processing_tasks.py:80-82)
Task sets `document.processing_status = PROCESSING` (:80). Except path fails only the job — document status never reset to FAILED. `sweep_stuck_processing_jobs` (processing_tasks.py:1199) fails ProcessingJob rows only, never documents. Document shows "processing" forever in UI, and content-hash dedup sees a live non-terminal row → re-upload of the same file blocked. Contrast: `process_document_ingestion` correctly resets the document (processing_tasks.py:472-479).

### R2-H8 — task_acks_late=True without task_reject_on_worker_lost=True (celery_app.py:77)
Celery default: child process killed (OOM-kill, worker_max_memory_per_child edge) → message ACKED anyway, never redelivered. The replay-guard architecture's premise (replay_guard.py:3-7: "worker killed mid-run never acks its message and the broker redelivers it") is false for the child-kill case. OOM-evicted child silently loses the task; job sits RUNNING until 30-min sweeper fails it; user waits 30 min for a failure that already happened. All the FOR UPDATE claim logic guards a redelivery that mostly won't come (only whole-pod/connection loss redelivers).

### R2-H9 — score normalization scale mismatch breaks fusion + confidence gate (hybrid_search_service.py:876-889, +1114-1120, 235)
SQL builds `ts_rank_cd(...) * 10` (fulltext_search_service.py:235); ts_rank_cd typically 0.05–0.1 → relevance 0.5–1.0. `_normalize_score` does `min(score/50.0, 1.0)` assuming max 50 → normalized 0.001–0.02, weighted ≈ 0.004–0.008. KG arm: 0.8/10 = 0.08 × 0.2 = 0.016. Fused ranking dominated by fixed boosts (recency ≤0.1, diversity ≤0.1) — a doc uploaded <24h outranks a top lexical match 10× over. `_calculate_deterministic_confidence` averages these tiny fused scores → confidence ≈ 0.02–0.15 → `_apply_deterministic_gate` (api/search/search.py:137-141, threshold 0.2) returns NO_MATCH for good result sets whenever Cohere rerank is disabled OR falls back (`_fallback_rerank` preserves original tiny scores).

### R2-H10 — KG entity UUID passed as document_id → INSUFFICIENT_EVIDENCE on every populated hybrid response (hybrid_search_service.py:662-664)
KG results use `str(result.id)` (Neo4j entity UUID) as `document_id`; fulltext uses Document UUIDs. Different namespaces → `_fuse_search_results` (:743) can never merge the same doc across sources → `source_count` always 1 → `coverage` always 0.0 → `_apply_deterministic_gate` (api/search/search.py:143-147) fires INSUFFICIENT_EVIDENCE on every populated `/hybrid` response (and endpoint at :866). Diversity boost (:779-782) is dead code. Secondary: entity UUID returned as document_id → client "open document" 404s; `_enrich_titles_from_db` never resolves them.

### R2-H11 — DO KB backfill _next_batch missing is_deleted filter (backfill.py:151-161)
Query selects any doc with `do_kb_data_source_uuid IS NULL` — includes soft-deleted docs (delete path only unsyncs when uuid was set; docs deleted before sync, or whose unsync failed, get ingested) and PENDING/FAILED docs. Tenant-deleted content re-ingested into DO KB → still retrievable via RAG node after user deleted it. Privacy regression. `reprovision_org` (:90) filters `is_deleted` — the batch loop doesn't.

---

## MEDIUM

### R2-M1 — top-20 scan window → systematic false deletions (arxiv_change_tracker.py:292-331 + :628)
`track_category_changes` fetches max_results=20 sorted submittedDate desc per category. Any tracked paper that slides below rank 20 (newer papers arrive) → `miss_count` increments → after 3 runs marked `deleted` (:310-323) → `_mark_paper_deleted` stamps `document_metadata["deleted"]=True` (:589-598). The DELETION_MISS_THRESHOLD comment (:65-68) names this exact failure then commits it anyway. Zero consumers of `md["deleted"]` found, so damage limited to state pollution + phantom summary counts + paper dropped from future tracking.

### R2-M2 — revision update desyncs DB text from durable artifact (arxiv_change_tracker.py:536-570)
When a title/abstract update does apply, `content_text` is overwritten but `checksum_sha256`, `file_size_bytes`, and the S3 object are left at the old revision (no storage re-promotion). `content_text` (new) vs checksum/S3 PDF (old) disagree; a later re-ingest of the same arxiv_id hits `existing_arxiv_document` → `_has_durable_storage(existing)` True on the stale checksum → reuse path keeps serving the old artifact indefinitely.

### R2-M3 — cancel is cosmetic; cancelled draft still commits (draft_generation_service.py:760-776)
`cancel_generation` only flips the in-memory status dict. The asyncio.Task from `_fire_and_forget` (:82-87) is never cancelled — no task_id→task map exists. User cancels → status shows CANCELLED → background task's next `_update_status` overwrites back to CITING/FINALIZING → draft fully persisted and marked COMPLETED after user was told it was cancelled.

### R2-M4 — failed PDF download persisted as mislabeled .pdf (arxiv_service.py:902-913 + storage.py:61)
`document_type`/`mime_type`/`filename` chosen from link presence, not download success. On download/extract failure the except (:885-887) only sets `pdf_extraction_failed`; `store_arxiv_pdf` takes the text branch (no pdf_path) but inherits mime_type="application/pdf" + filename="{paper_id}.pdf" → abstract-only UTF-8 bytes committed to S3 as application/pdf. Downstream PDF consumers (preview render, DO KB pre-flight) fail on the file.

### R2-M5 — sync Neo4j calls block the event loop (arxiv_kg_integration.py:751, 776, 813)
`KnowledgeGraphService.create_entity`/`search_entities`/`create_relationship` are synchronous (own get_session, circuit-breaker checks, network I/O) and called from async contexts without to_thread. Per paper: N entity writes + M relationship lookups, each a separate session. Entire event loop stalls per call — concurrent requests on the same worker stall; slow/half-open Neo4j amplifies via the circuit-breaker path.

### R2-M6 — zip(papers, documents) misaligns on partial ingest failure (arxiv_kg_integration.py:105)
`ingest_papers` drops failed papers from `documents` (arxiv_service.py:803-810). `zip` then pairs each surviving doc with the wrong paper's metadata → entities/relationships stamped with a foreign paper_id/paper_title in metadata. Latent (no live caller) but exactly the partial-state trap.

### R2-M7 — quota check-then-act race, no DB backstop (file_service.py:213-224 + files.py:199-203, organization.py:115-157)
`can_upload_file` reads `storage_used_bytes` from the request-loaded ORM instance (stale, no lock); `storage_usage_update` increments unconditionally; `organizations` table has NO `storage_used_bytes <= storage_limit_bytes` CHECK constraint (one exists only in the unapplied alt model indexing_strategy.py:300-302). N concurrent uploads each pass against the same stale value, all increment → org permanently over quota.

### R2-M8 — concurrent summarize race: no lock, late rate key, force bypass (thread_summarization_service.py:183-263)
No SELECT FOR UPDATE, no version column. Redis rate-limit key set only AFTER generation finishes (:232/240/248) — dedup window = full LLM latency; two `.delay`s enqueued before either finishes both run. `force=True` tasks (summarize_thread_on_resolve_task, bulk summarize, manual POST summarize) bypass rate limit entirely. (a) duplicate LLM spend per race; (b) out-of-order completion — task A reads messages at t1, B at t2, B commits first, A last → thread.summary regresses to older conversation state.

### R2-M9 — sync S3+PDF work on the shared event loop; task object runs inline in API pod (multimodal_processing_service.py:836-838 + document_upload.py:49,390-392, tasks/_async_utils.py:74-83)
`extract_text_content` is async but calls sync `FileService.extract_text_content` (S3 download-to-tempfile + pypdf/pandas parse) directly — no asyncio.to_thread, unlike agent tools. `run_async` hosts every coroutine on ONE daemon-thread loop per process. Worse: document_upload.py:390 passes the Celery task OBJECT to `background_tasks.add_task` → `Task.__call__` runs the whole processing body inline in the API process (not dispatched to Celery) → the shared loop lives in the API pod; a single 1GB PDF extraction freezes every other run_async coroutine there. Concurrent uploads serialize; WebSocket progress stalls for the full extraction; `self.request.id` is None so the job row's worker identity is bogus.

### R2-M10 — blanket except rewraps route's own HTTPExceptions (files.py:240-241)
The 403 permission failure (:182) and 413 quota failure (:200) raised inside try are caught and re-raised as HTTPException(400) → client receives 400 {"detail": "403: ..."}. Wrong status codes for clients/monitoring; quota-overage retries keyed on 413 never trigger. (cancel_upload at :695-696 does `except HTTPException: raise` correctly.)

### R2-M11 — summary prompt keeps the OLDEST ~2000 chars of a thread (thread_summarization_service.py:149-181, 215)
Messages ordered created_at.asc(); loop breaks at max_chars=2000. Prompt demands "current status" (:82) but recent messages are dropped first. Long threads (primary resolve-time use case) get summaries describing only their opening turns — systematically stale, worse as threads grow.

### R2-M12 — cleanup_old_evaluations FK violation, nightly task fails forever (evaluation_tasks.py:623-647)
`EvaluationJob` ORM cascades cover metrics/datasets only (evaluation.py:103-108). `evaluation_reports.job_id` (:349-351) and `evaluation_comparisons.baseline_job_id/comparison_job_id` (:309-310) have FKs with NO ondelete and no relationship. `db.delete(job)` defers to flush at commit (:647) — OUTSIDE the per-job try/except. One old job with a report/comparison → IntegrityError → whole batch rolls back → beat task fails every night permanently.

### R2-M13 — sweeper fails run when live-store READ fails (agent_run_tasks.py:362-390) [H2 residual]
Sweeper otherwise hardened: checks live store (get_job_fresh) before sweeping, repairs projection from terminal live status, lease-claims atomically, re-reads under lease. Residual: if get_job_fresh raises (Redis blip at sweep moment, :364-371) → job=None, live_status=None → row FAILED "Swept as stale". A run that actually completed whose projection write was earlier lost to a PG blip gets its real error destroyed. Requires double failure (PG blip at completion + Redis blip at sweep) — narrow but real.

### R2-M14 — extract_entities guard covers only post-COMPLETED redelivery (processing_tasks.py:553-666, latent)
Worker killed mid-run (entities committed :636, complete_job not yet) → redelivery: status RUNNING passes the COMPLETED-only guard → start_job regresses → full paid LLM re-extraction → appends SECOND copy of every OPENAI Entity row (comment :567-569 admits OPENAI rows deliberately never delete-before-inserted). No live dispatcher currently — latent.

### R2-M15 — rerank truncates candidate pool pre-pagination; totals lie (hybrid_search_service.py:193-198, 995, 1037-1041, 1056)
Rerank called with top_n = limit * 2; dropped docs removed from fused_results. `_apply_final_filtering` computes total_filtered = len(pool) ≤ limit*2 (≤40 without rerank: max_results_per_source=20 × 2 arms). With limit=10, offset ≥ 20 returns empty page + has_more=False while fulltext matched 100s. total_results silently changes (40→20) when Cohere toggles on. Deep pagination dead on hybrid.

### R2-M16 — quality analytics endpoint serves fabricated metrics; feedback never persisted (search_quality_service.py:395-428, 434-443)
`get_quality_analytics` returns hardcoded mock ("total_evaluations": 150, fake averages) — wired live at api/search/search_quality.py:214. `_store_metric` only logs; `record_user_feedback` returns "Feedback recorded successfully" and discards the data.

### R2-M17 — benchmark serialization accesses nonexistent fields → 500 (search_quality.py:272-274)
`result.score_breakdown`, `result.source_type`, `result.highlights` on SearchResult (models/search_schemas.py:98-119 — plain BaseModel, no such fields) → AttributeError → 500 whenever benchmark returns any results.

### R2-M18 — VECTOR 400 swallowed, re-raised as 500 (api/search/search.py:200-206 + 245-247)
`raise HTTPException(400)` for vector search sits inside try whose broad except Exception catches HTTPException → client gets 500 instead of the intended 400.

### R2-M19 — backfill dry_run mutates and commits progress state (backfill.py:209-215, 285-287)
progress.status = "in_progress" + started_at committed before the loop regardless of dry_run; end commits status="dry_run_done" + finished_at. "Dry run" is not read-only; consumers switching on status hit an unexpected value.

### R2-M20 — backfill failed docs permanently skipped (backfill.py:237-249, 190-207)
Failed sync leaves do_kb_data_source_uuid NULL but cursor advances past them; run finishes status="completed" → re-runs short-circuit. Never writes do_kb_sync_status='failed', so reconcile re-driver can't pick them up. N docs silently missing from KB after transient DO outage; only manual reprovision_org (nukes whole KB) recovers.

### R2-M21 — sync_document_to_kb commits caller's transaction mid-call (do_kb/ingest.py:234-242)
Commits the shared AsyncSession mid-call — flushes/commits any unrelated pending changes in the caller's pipeline. Also makes backfill.py:255-259's "commit once per BATCH (not per document)" comment false — every doc commit happens inside sync anyway.

---

## LOW

### R2-L1 — `match[0]` takes the first character (arxiv_kg_integration.py:874)
re.findall with one group returns the group strings; `match[0].split(",")[0]` yields a single character. cited_papers = list of one-letter strings instead of author names.

### R2-L2 — `_generation_status` unbounded (draft_generation_service.py:42, 157)
Entries added per generation, never pruned. Slow memory growth per pod; cross-pod invisible so active_only dedup is process-local only (DB uq constraint covers the race).

### R2-L3 — `[Doc 0]` cites the last document (draft_generation_service.py:661-677)
`doc_idx = int(match) - 1`; LLM emitting `[Doc 0]` → -1 → `documents[-1]` silently cited. Only `doc_idx < len` checked, not `>= 0`.

### R2-L4 — tracker state file pod-local, lock process-local (arxiv_change_tracker.py:72)
_STATE_LOCK (threading) + os.replace guard only same-process writers. HPA 1-5 + synthetic CronJob → concurrent scans on different pods diverge state; last writer wins per pod-local file. DB dedup absorbs duplicate "new" processing, but updated-hash advancement on one pod can suppress the change on another.

### R2-L5 — deleting the current draft leaves none current (draft_generation_service.py:890-904)
delete_draft doesn't reassign is_current to the next-highest version; get_draft(current_only=True) returns None until a new generation.

### R2-L6 — selectinload(Collection.documents) unbounded per project (project_service.py:81)
50 projects × unbounded junction+document rows loaded for a list view. Perf only.

### R2-L7 — timeout fallback returned but never persisted (thread_summarization_service.py:251-254)
On asyncio.TimeoutError, `_generate_fallback_summary` result returned WITHOUT `_update_thread_summary`. Caller logs "Generated summary"; thread.summary stays stale/None.

### R2-L8 — worker Redis client has no socket timeouts (thread_summarization_service.py:104-107 vs :50-55)
Pre-check client sets 2s connect/socket timeouts; the worker's redis_client doesn't. Redis stall mid-command blocks the Celery task forever — unretryable, worker slot pinned.

### R2-L9 — `thread.message_count += 1` read-modify-write (chat_service.py:849-850 + agent_execution_service.py:1925)
Not atomic UPDATE. Concurrent message creates lose increments → counter drifts below MIN_MESSAGES_FOR_SUMMARY threshold (delays/skips summarization) and token accounting undercounts.

### R2-L10 — stored mime_type trusts filename; document_type uses magic bytes (file_service.py:270-276, 358, 375)
validate_file returns extension-derived mime while get_file_type sniffs content — text/html body named .txt stores text/plain and the S3 object is uploaded with that claimed Content-Type, then served via presigned URL. Content-type spoof gap.

### R2-L11 — .zip/.rar allowed but have no extraction path (file_service.py:257-258, 198, 658-659)
Falls to DocumentType.MULTIMODAL → `_extract_text_from_path` returns "" → pipeline marks COMPLETED/indexed with empty content. Every zip upload becomes a silently empty document.

### R2-L12 — full-file `await file.read()` into memory (file_service.py:371, 404)
Entire upload buffered as one bytes object (twice if save_file_to_storage used) — bounded only by tier max (1GB enterprise) per concurrent request. Memory-spike vector.

### R2-L13 — base upload path writes content hash but never checks it (file_service.py:464-466 + files.py:211-218)
upload_file stores file_hash in metadata; dedup lives only in the check-duplicate endpoint + enhanced upload path. The live POST /api/v1/documents/files/upload route creates duplicate Document rows + double quota for identical content.

### R2-L14 — per-call AsyncOpenAI/AsyncAnthropic clients never closed (thread_summarization_service.py:272, 313)
New client per generation; no async with/.close(). Connection churn + ResourceWarnings at summarization volume.

### R2-L15 — high/low priority tasks: no claim + fail-then-retry + fixed worker_id (document_processing_tasks.py:166-309, dead code)
`process_high_priority_document`/`process_low_priority_document` lack claim_job_for_processing entirely (acks_late redelivery mid-run = double full-document processing), constant worker_id="high_priority_worker", repeat fail_job-then-retry. No dispatcher — dead paths, latent risk if revived.

### R2-L16 — execute_research_workflow: no idempotency, no time-limit override (research_tasks.py:206-377, dead code)
No terminal-state guard → mid-run redelivery re-runs full workflow, appends duplicate ResearchStep rows. Global task_soft_time_limit=300 kills any multi-step LLM+arXiv workflow at 5 min. No dispatcher — dead task.

### R2-L17 — text_processing / vector_processing queues consumed by nobody (processing_service.py:133,137)
Worker consumes `celery,document_processing,high_priority,low_priority,entity_processing,graph_processing,agent_runs` (celery-worker-deployment.yaml:52). `queue_processing_job` publishes TEXT_EXTRACTION→text_processing, EMBEDDING_GENERATION→vector_processing → job stuck QUEUED until stuck-sweeper fails it 30 min later. Inverse: worker consumes entity_processing/graph_processing which are not declared in task_queues (celery_app.py:86-107). Latent.

### R2-L18 — health_check / update_processing_metrics leak session on exception (document_processing_tasks.py:609-641, 670-710)
db.close() on happy path only; exception path leaks the session. health_check runs every 5 min via beat — repeated DB outages leak connections in worker.

### R2-L19 — beat schedule uses placeholder org id (document_processing_tasks.py:660-664)
`generate-reports` args ("default_organization_id", 30) — literal placeholder; daily task queries a nonexistent org, always returns empty stats.

### R2-L20 — `" & "` join fed to plainto_tsquery (fulltext_search_service.py:345-346)
plainto_tsquery doesn't parse operators; & stripped as punctuation. AND semantics hold by accident. Latent trap if anyone switches to to_tsquery. Dead code.

### R2-L21 — suggestion LIKE pattern unescaped (fulltext_search_service.py:548)
`f"%{query}%"` — user %/_ act as wildcards in suggestion matching. Param-bound (no injection), but nonsense suggestions / scan amplification.

### R2-L22 — Cohere client: no reuse, no 429 backoff (cohere_rerank_service.py:153, 329)
Fresh httpx.AsyncClient()/Client() per call (full TLS setup every rerank); 429 single-shot failure recorded to circuit breaker, no Retry-After handling. Burst of 429s opens breaker → extended fallback windows.

### R2-L23 — DO KB client: retry tail sleeps before raising; JSON decode uncaught (do_kb/client.py:126-144, 152)
Last attempt on retryable status still sleeps (up to 60s capped Retry-After) before raising "exhausted retries". response.json() failure raises JSONDecodeError, not mapped to DOKnowledgeBaseError — bypasses 404-vs-transient classification in retrieve_kb_chunks.

### R2-L24 — bm25 update_statistics accumulates corpus state (bm25_service.py:167-190)
doc_count/doc_freqs accumulate across calls (double-counted corpus → wrong IDF); avg_doc_length divides current-batch length by cumulative count. Dead code (no importers).

### R2-L25 — hybrid suggestions always empty (hybrid_search_service.py:1131-1138)
`hasattr(source_result, "suggestions")` — SearchSourceResult defines no suggestions field → dead branch, hybrid never returns suggestions.

### R2-L26 — entity-indicator substring matching (hybrid_search_service.py:471-481)
`"who" in query` matches "whole", "how" matches "showcase" → KG arm spuriously included. Cost/latency only given R2-H10 (fusion no-op).

---

## Verified NOT buggy (round 2)

- **DraftGenerationService session usage** — `_generate_draft_async` opens its own AsyncSessionLocal() windows (:240, :306); self.db never touched in background path.
- **arXiv UUID document_ids** — persist_arxiv_documents returns persisted Document.id UUIDs.
- **Rate gate/timeouts** — cross-pod Redis slot gate with per-process fallback; 429 fail-fast honoring clamped Retry-After; PDF fetch 300s; API 20s.
- **Storage compensation** — promotion-before-transaction with full promoted_storage manifest, rollback cleanup, race-loser discarded_storage handling.
- **Sync/async boundaries** — get_async_session proper asynccontextmanager; begin_nested contains per-paper IntegrityError races.
- **Tenant scope** — persistence.py, _paper_lookup_stmt, tracker state partitioning, draft _build_project_documents_query, fulltext org filter, KG arm org threading, suggestions org-scoped fail-closed, DO KB resolve_and_filter_chunks org-scoped + fail-closed.
- **Fulltext count/result parity** — _build_search_query vs _build_count_query identical WHERE; has_more math consistent.
- **plainto_tsquery** — no tsquery injection.
- **do_kb/rerank.py** — index bounds/validation + full-coverage check + strict passthrough on failure.
- **do_kb/postprocess.py** — dedup fingerprinting + order retention correct; PII redact applied.
- **Cohere fallback ordering** — _fallback_rerank preserves input order; doc_ids[idx] mapping correct sync+async.
- **Summarization sync/async boundary** — Celery task uses SessionLocal + asyncio.run; API route uses to_thread with own sync session. No AsyncSession crosses into ThreadSummarizationService.
- **Agent tool extraction offload** — tools_impl offloads extract_text_content via to_thread (violation is R2-M9, different service).
- **Upload failure compensation ordering** — idempotent, correctly ordered.
- **expire_on_commit=False** — post-commit attribute access safe.
- **Queue routing (live tasks)** — all 4 routed tasks land on consumed queues; beat tasks on default queue consumed.
- **Task sessions** — fresh SessionLocal per task, closed in finally (except R2-L18); run_async shared loop PID-guarded fork-safe.
- **Sweeper core** — lease-claim serializes, re-read under lease, live-store cross-check + repair, absorbing-terminal upsert, awaiting window 7200s > Redis job TTL 3600s.
- **sweep_stuck_processing_jobs** — mark-failed-only, terminal-aware, flag-gated.
- **retention_tasks FKs** — CASCADE/SET NULL correct, no orphans, DB-rows-only, two-stage dry-run gate.
- **reconcile_tasks** — report-only default, per-doc commits, SoftTimeLimitExceeded caught, shared count/list filters.
- **run_agent_job** — one-shot lease claim blocks double-run; no autoretry (correct); wrapper-level fail-write prevents spinning pollers.
- **kg_extract/kg_merge/evaluation tasks** — terminal-state redelivery guards present.
