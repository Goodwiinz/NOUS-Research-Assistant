# Agent audit round 2 — started 2026-08-17
Source: opencode session, 4 parallel explore agents (arxiv/draft/project services, file/summarize services, Celery workers, RAG retrieval core)
Detailed findings: ~/.audit-ledgers/rag/agent-audit-round2-details.md

| ID | Finding (one line) | Sev | Status | Owner | PR | Updated |
|----|--------------------|-----|--------|-------|----|---------|
| R2-H1 | tracker KG integration omits organization_id → entities MERGE into shared ""-org partition, cross-tenant KG mixing (arxiv_change_tracker.py:511,577) | high | fixed | clawd | #1452 | 08-18 |
| R2-H2 | delete_project hard-DELETE hits RESTRICT FKs (project_skills, agent_runtime_snapshots) → IntegrityError 500; violates soft-delete convention (project_service.py:228-234) | high | fixed | clawd | #1455 | 08-18 |
| R2-H3 | fields_changed built only from title/authors/category — abstract updates never detected; new hash persisted, text/search vector never updated, change never re-emitted (arxiv_change_tracker.py:246-259,536-544) | high | fixed | clawd | #1452 | 08-18 |
| R2-H4 | FileService() built without required db arg in do_kb pre-flight → TypeError swallowed → forced text extraction NEVER runs (pre_flight.py:75) | high | fixed | clawd | #1503 | 08-19 |
| R2-H5 | fail_job(committed FAILED) before self.retry → claim guard sees terminal → retry machinery self-destructs; transient error = permanent fail (document_processing_tasks.py:127-153) | high | fixed | clawd | #1453 | 08-18 |
| R2-H6 | no db.rollback() before fail_job in except → PendingRollbackError masks self.retry; job stuck RUNNING til sweeper (document_processing_tasks.py:127-133) | high | fixed | clawd | #1453 | 08-18 |
| R2-H7 | document never leaves PROCESSING on task failure; sweeper fails jobs not docs; dedup blocks re-upload (document_processing_tasks.py:80-82) | high | fixed | clawd | #1453 | 08-18 |
| R2-H8 | task_acks_late=True without task_reject_on_worker_lost=True → OOM-killed child acks anyway, redelivery premise false (celery_app.py:77) | high | fixed | clawd | #1454 | 08-18 |
| R2-H9 | score normalization scale mismatch: fulltext SQL emits ~0.5-1.0, normalizer divides by 50 → fused ranking dominated by recency/diversity boosts; confidence ~0.02-0.15 → deterministic gate NO_MATCH on good results when rerank off/fallback (hybrid_search_service.py:876-889) | high | fixed | clawd | #1456 | 08-18 |
| R2-H10 | KG arm passes entity UUID as document_id → cross-source fusion impossible → coverage always 0 → INSUFFICIENT_EVIDENCE gate fires on every populated hybrid response; entity UUID 404s on open-doc (hybrid_search_service.py:662-664) | high | fixed | clawd | #1456 | 08-18 |
| R2-H11 | DO KB backfill _next_batch missing is_deleted (+status) filter → tenant-deleted docs re-ingested, still retrievable (backfill.py:151-161) | high | fixed | clawd | #1451 | 08-18 |
| R2-M1 | top-20 scan window: papers sliding below rank 20 → miss_count → falsely marked deleted (arxiv_change_tracker.py:292-331,628) | med | open | — | — | 08-17 |
| R2-M2 | revision update overwrites content_text but leaves checksum/S3 PDF at old revision → permanent artifact desync on reuse path (arxiv_change_tracker.py:536-570) | med | open | — | — | 08-17 |
| R2-M3 | cancel_generation cosmetic: task never cancelled, status overwritten back to CITING/FINALIZING, cancelled draft fully persists (draft_generation_service.py:760-776) | med | open | — | — | 08-17 |
| R2-M4 | failed PDF download persisted as mislabeled application/pdf (abstract bytes as .pdf) (arxiv_service.py:902-913) | med | refuted | — | — | 08-18 |
| R2-M5 | sync Neo4j calls block event loop per entity/relationship during KG integration (arxiv_kg_integration.py:751,776,813) | med | open | — | — | 08-17 |
| R2-M6 | zip(papers, documents) misaligns on partial ingest failure → KG stamped with foreign paper metadata (arxiv_kg_integration.py:105) | med | open | — | — | 08-17 |
| R2-M7 | quota check-then-act race: stale read + unconditional increment, no CHECK constraint → org permanently over quota on concurrent uploads (file_service.py:213-224) | med | open | — | — | 08-17 |
| R2-M8 | concurrent summarize race: no lock/version, rate key set post-completion, force bypasses → duplicate LLM spend + summary regression via out-of-order commit (thread_summarization_service.py:183-263) | med | open | — | — | 08-17 |
| R2-M9 | minutes-long sync S3+PDF extraction on shared process loop; document_upload passes Celery task OBJECT to background_tasks → runs inline in API pod (multimodal_processing_service.py:836-838, document_upload.py:390) | med | fixed | clawd | #1453 | 08-18 |
| R2-M10 | blanket except rewraps own HTTPExceptions → 403/413 become 400 (files.py:240-241) | med | open | — | — | 08-17 |
| R2-M11 | summary prompt keeps OLDEST ~2000 chars — long threads summarized from opening turns only (thread_summarization_service.py:149-181) | med | open | — | — | 08-17 |
| R2-M12 | cleanup_old_evaluations: reports/comparisons FKs without ondelete → nightly batch IntegrityError forever (evaluation_tasks.py:623-647) | med | open | — | — | 08-17 |
| R2-M13 | sweeper residual: get_job_fresh raise (Redis blip) → row FAILED "Swept as stale", real error destroyed — needs double failure (agent_run_tasks.py:362-390) | med | open | — | — | 08-17 |
| R2-M14 | extract_entities guard covers COMPLETED-redelivery only; mid-run kill → RUNNING passes → duplicate OPENAI entity rows (processing_tasks.py:553-666, latent) | med | open | — | — | 08-17 |
| R2-M15 | rerank truncates pool to limit*2 pre-pagination → offset≥20 empty pages, total lies, deep pagination dead on hybrid (hybrid_search_service.py:193-198,995+) | med | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-M16 | quality analytics endpoint returns hardcoded mock metrics; user feedback never persisted (search_quality_service.py:395-443) | med | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-M17 | benchmark serialization accesses nonexistent SearchResult fields → 500 on any non-empty result (search_quality.py:272-274) | med | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-M18 | VECTOR 400 swallowed by broad except → re-raised 500 (search.py:200-247) | med | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-M19 | backfill dry_run mutates + commits progress state ("dry_run_done") — not read-only (backfill.py:209-215) | med | fixed | clawd | #1451 | 08-18 |
| R2-M20 | backfill failed docs permanently skipped: cursor advances, no failed status, reconcile can't pick up (backfill.py:237-249) | med | fixed | clawd | #1451 | 08-18 |
| R2-M21 | sync_document_to_kb commits shared session mid-call → commits caller's unrelated pending changes (do_kb/ingest.py:234-242) | med | open | — | — | 08-17 |
| R2-L1 | re.findall match[0] takes first CHARACTER — cited_papers = one-letter strings (arxiv_kg_integration.py:874) | low | open | — | — | 08-17 |
| R2-L2 | _generation_status dict unbounded, process-local (draft_generation_service.py:42,157) | low | open | — | — | 08-17 |
| R2-L3 | [Doc 0] → idx -1 → last document silently cited (draft_generation_service.py:661-677) | low | open | — | — | 08-17 |
| R2-L4 | tracker state file pod-local + process-local lock; HPA pods diverge (arxiv_change_tracker.py:72) | low | open | — | — | 08-17 |
| R2-L5 | delete_draft of current leaves no current; get_draft(current_only) returns None (draft_generation_service.py:890-904) | low | open | — | — | 08-17 |
| R2-L6 | selectinload(Collection.documents) unbounded per project in list view (project_service.py:81) | low | open | — | — | 08-17 |
| R2-L7 | timeout fallback summary returned but never persisted (thread_summarization_service.py:251-254) | low | open | — | — | 08-17 |
| R2-L8 | worker Redis client lacks socket timeouts → Redis stall pins Celery slot forever (thread_summarization_service.py:104-107) | low | open | — | — | 08-17 |
| R2-L9 | thread.message_count/token_count read-modify-write — concurrent creates lose increments (chat_service.py:849) | low | open | — | — | 08-17 |
| R2-L10 | stored mime_type trusts filename while type sniffs magic bytes; S3 Content-Type spoof (file_service.py:270-276) | low | open | — | — | 08-17 |
| R2-L11 | .zip/.rar accepted, no extraction path → silently empty COMPLETED docs (file_service.py:257) | low | open | — | — | 08-17 |
| R2-L12 | full file.read() into memory (up to 1GB) per upload (file_service.py:371,404) | low | open | — | — | 08-17 |
| R2-L13 | base upload path writes file_hash but never checks it — duplicates + double quota (files.py:211-218) | low | open | — | — | 08-17 |
| R2-L14 | per-call AsyncOpenAI/AsyncAnthropic clients never closed (thread_summarization_service.py:272,313) | low | open | — | — | 08-17 |
| R2-L15 | high/low priority tasks: no claim guard, fail-then-retry, fixed worker_id (document_processing_tasks.py:166-309, dead code) | low | fixed | clawd | #1453 | 08-18 |
| R2-L16 | execute_research_workflow: no idempotency, soft-limit 300s kills multi-step flows (research_tasks.py:206-377, dead code) | low | open | — | — | 08-17 |
| R2-L17 | text_processing/vector_processing queues published but consumed by nobody → jobs stuck til sweeper (processing_service.py:133,137) | low | open | — | — | 08-17 |
| R2-L18 | health_check/metrics leak session on exception path; beat every 5min (document_processing_tasks.py:609-710) | low | fixed | clawd | #1454 | 08-18 |
| R2-L19 | beat generate-reports uses placeholder org id "default_organization_id" (document_processing_tasks.py:660-664) | low | pr | sess:audit-r6 | #1529 | 08-22 |
| R2-L20 | " & " join fed to plainto_tsquery — operators stripped, AND by accident (fulltext_search_service.py:345) | low | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-L21 | suggestion LIKE pattern unescaped % _ (fulltext_search_service.py:548) | low | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-L22 | Cohere client: fresh httpx per call, 429 single-shot no Retry-After (cohere_rerank_service.py:153,329) | low | open | — | — | 08-17 |
| R2-L23 | DO KB client: retry tail sleeps before raise; JSON decode errors unmapped (do_kb/client.py:126-152) | low | open | — | — | 08-17 |
| R2-L24 | bm25 update_statistics accumulates corpus state — wrong IDF (dead code) (bm25_service.py:167-190) | low | open | — | — | 08-17 |
| R2-L25 | hybrid suggestions always empty — hasattr on nonexistent field (hybrid_search_service.py:1131-1138) | low | pr | sess:audit-r6 | #1530 | 08-22 |
| R2-L26 | entity-indicator substring matching ("who" in "whole") spuriously arms KG (hybrid_search_service.py:471-481) | low | pr | sess:audit-r6 | #1530 | 08-22 |

| R2-M22 | api/arxiv/arxiv_knowledge_graph.py:218 calls ingest_papers(extract_entities=...) — kwarg doesn't exist → TypeError 500 on every bulk-ingest request | med | fixed | clawd | #1452 | 08-18 |

## Log
- 2026-08-18 (backfill): merged-fix status reconciled from commit bodies — H1/H3/M22 (#1452), H2 (#1455), H5/H6/H7/L15/M9 (#1453, dead upload cluster deleted), H8/L18 (#1454), H9/H10 (#1456), H11/M19/M20 (#1451). M4 REFUTED by the verify pass (storage.py re-derives filename/mime from pdf_path; the text branch stores .txt/text-plain). M9 was downgraded to a deletion candidate before #1453 removed the cluster outright. R2-M22 row added — it was filed during the verify pass, after this file was first committed. 40 rows remain open, R2-H4 the only high.
- 2026-08-17 (verify pass): all 58 findings adversarially re-verified against head 9782d793 — 56 CONFIRMED, M4 refuted, M9 downgraded. Nuances worth keeping: H5's retry path was unreachable anyway (process_document_upload only reachable via an unmounted router); H9 masks H10 (the confidence gate fires first) so they had to be fixed together, and Cohere is off in the deployed config, making the NO_MATCH branch the live one; H11/M19/M20/M21 are all gated on DO_KB_ENABLED, and H11 has a data-exposure shape, so it had to land before that flag flips; M21's live impact is ~nil (every caller passes a dedicated session) but multimodal_processing_service.py:1163 passes a SYNC Session into the async sync_document_to_kb — a separate latent bug; M12 is conditional on the evaluation_reports/comparisons tables, which have no create migration (same debt class as the analytics tables).
- 2026-08-17: #1452 also fixed a third untracked H1 call site (/force-sync route, arxiv_change_tracking.py:309), caught by mypy. #1456 root-caused a third bug beyond H9/H10: the hybrid service imported the knowledge_graph_service submodule rather than the singleton (the package __init__ never re-exports it), so the KG arm was silently dead in prod behind a swallowed AttributeError. Coverage was redefined as content-backed (KG is enrichment, not evidence). Follow-up filed but not tracked as a finding: redelivered-aware reclaim in claim_job_for_processing, to bypass the 900s young-RUNNING skip when delivery_info.redelivered is set.
- 2026-08-17: round 2 created. 58 findings (11 high, 21 med, 26 low). R1's L10 (project_service ilike) re-verified still live — kept under R1, not duplicated. Verified-clean highlights: DraftGenerationService session usage, arXiv UUID document_ids, storage compensation on upload failure, summarization sync/async boundary, tenant scope in fulltext/KG/DO KB resolve, count/result filter parity in fulltext, retention FK orphans, sweeper lease/cross-check core (R2-M13 is residual only), replay-guard claim logic (defeated instead by R2-H5/H8).
