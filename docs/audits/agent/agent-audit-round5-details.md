# Agent audit round 5 — detailed findings — 2026-08-18
Source: opencode session, 4 explore agents (KG services+API, threads/workspaces, research/evidence/eval, scripts+infra/CI)
Status ledger: ~/.audit-ledgers/rag/agent-audit-round5.md

---

## HIGH

### R5-H1 — arXiv KG API: dependency never initialized + nonexistent methods — endpoints never worked (arxiv_knowledge_graph.py:56-64 + 92-97,251-261,295-303,336-344,383-389)
`get_kg_integration` returns `ArXivKnowledgeGraphIntegration()` — `__init__` (arxiv_kg_integration.py:38) leaves `kg_service=None`; only `__aenter__` initializes. Dependency never enters the context manager:
- `/entity/{name}`, `/path/{source}/{target}`, `/stats`: `if not kg_integration.kg_service` → always 503
- `/subgraph`: `create_paper_kg_subgraph` → always error → 404
- `/bulk-ingest`: `process_paper_kg_integration` skips every KG write (`if self.kg_service:` false at :1116) but returns success dict → caller reports N KG entries created while writing ZERO — and pays LLM extraction cost per paper
Secondary: even if initialized, those endpoints call `get_entity_details` / `find_shortest_path(source_entity=...)` / `get_graph_statistics()` — methods that don't exist on KnowledgeGraphService (and sync, awaited) → AttributeError. VERIFIED in code.

### R5-H2 — arxiv_local_batch: nonexistent method signatures, swallowed → SSE "completed", KG never written (arxiv_local_batch.py:106-135)
Calls `create_entity(entity_type=..., name=..., properties=...)` and `create_relationship(source_name=..., target_name=..., relationship_type=...)` — real signatures take request objects. TypeError per file swallowed at :135 → stream reports completed. Also zero org-scoping in file (current_user unused) — latent cross-tenant writer if "fixed" naively.

### R5-H3 — workspace detail 500s for EVERY workspace: MissingGreenlet (workspace_access.py:93 + presenters.py:108-109 + conversations.py:145-146)
`get_workspace` eager-loads only `Workspace.members` (not `members.user`); both detail presenters read `member.user.email/full_name` → lazy load under AsyncSession after commit-boundary → MissingGreenlight 500. Every workspace has ≥1 member (owner created at create_workspace:92-94) → guaranteed. Empirically verified with project venv. Frontend calls it (workspaceService.ts:88-90, used at :407). Codebase documents this exact failure at workspace_service.py:272-275 and export_service.py:445-450 (Sentry ticket) — detail paths missed. VERIFIED.

### R5-H4 — ADMIN can mint an irremovable OWNER (chat.py:90,102 + workspace_service.py:264-269,307-310,344-345)
WorkspaceMemberCreate/Update.role accept WorkspaceRole.OWNER; add_member + update_member_role write verbatim (only guard: target currently OWNER). Second OWNER member: full admin rights (can_user_admin), cannot be demoted or removed by anyone (both paths refuse role==OWNER rows) — DB surgery to undo.

### R5-H5 — EvaluationMetric.metadata doesn't exist → 500 + silent metadata loss (evaluation.py:469,473 + model :193 + rag_evaluation_service.py:214-296,1085)
Column is `metric_metadata`. Reads `metric.metadata.get(...)` → SQLAlchemy class-level MetaData → AttributeError → 500. Writers pass `metadata={...}` kwarg → declarative accepts (Base.metadata), flush drops → NULL forever (calculation_method, model_used provenance lost). Detailed reports render error string.

### R5-H6 — duplicate EvaluationDataset per job; unordered .first() → nondeterministic FAILED jobs (evaluation.py:181-197 + rag_evaluation_service.py:137-148 + evaluation_tasks.py:108-112)
Service creates dataset with questions=[] (API passes dataset=[]); API then inserts second with real questions. Task fetches `.first()` no order_by → heap order → empty row usually wins → total_items=0 → fail_job("No items were successfully processed").

### R5-H7 — next(get_db()) on async generator → batch eval permanently broken (rag_evaluation_service.py:641,650 + evaluation_tasks.py:152-161)
get_db is async (database.py:224); `next(async_gen)` → TypeError, swallowed per-item → `[]` for every query → batch jobs always FAILED "No queries were successfully evaluated". Triad jobs with no provided contexts: empty generated_answer/retrieved_context → LLM-judge scores on empty strings (fabricated) marked COMPLETED. VERIFIED.

### R5-H8 — opencode.yml pwn-request surface (.github/workflows/opencode.yml:11-18,29-31)
`issue_comment`/`pull_request_review_comment` created, gated only on body containing `/oc` — no actor/collaborator check. issue_comment workflows run in base-repo context WITH secrets even for fork-PR authors. Unpinned `anomalyco/opencode/github@latest` (mutable, vendor-controlled) receives OPENCODE_API_KEY + id-token: write. Tag hijack/vendor compromise = secret exfil + OIDC token forgery. VERIFIED.

---

## MEDIUM — KG

### R5-M1 — circuit breaker failure count never decays on success (circuit_breaker.py:140-171)
record_success() doesn't reset _failure_count in CLOSED; only full OPEN→HALF_OPEN→CLEARED cycle. 5 sporadic Neo4j blips accumulated over days → breaker opens for 30s rejecting all KG traffic; repeats on 6th historical failure.

### R5-M2 — parameter as variable-length bound = Cypher syntax error (graph_analytics_service.py:767)
`MATCH path = (start)-[*1..$max_depth]-(end)` — parameters not allowed as var-length bounds. /graph-analytics/paths type=all always fails (500 + failed-analysis row). Sibling graph_algorithms.py:529-532 documents constraint and clamps+interpolates instead.

### R5-M3 — paper-title entity never created → ALL paper-anchored edges dropped (arxiv_kg_integration.py:153-192,560-619,776-791)
_extract_entities_from_paper emits no Paper entity; _extract_relationships emits author→paper, paper→category, paper→cites. Endpoint resolution via search_entities(name, limit=1) → paper node never found → return. Every tracker/bulk ingest loses all author_of/belongs_to/cites edges.

### R5-M4 — sync Neo4j on event loop (arxiv_kg_integration.py:751,776,785,813 + arxiv_extraction.py:594-692)
Beyond known R2-M5 call sites: _add_entity_to_kg/_add_relationship_to_kg/_update_knowledge_graph_with_extractions async but call sync driver inline (no to_thread — contrast file_service.py:977, knowledge_graph_service_improved.py:50 which do it right). Bulk of 500 papers = thousands of sequential blocking calls on the loop.

### R5-M5 — relationship endpoints resolved by fulltext prefix, not exact name (arxiv_kg_integration.py:776-791 + arxiv_extraction.py:674-684)
search_entities uses entity_fulltext_idx token-prefix: "Attention" → "Attention Mechanism"; "Bert (author)" → "BERT" model. Edges wired to wrong entity nodes — silent graph corruption on every arXiv relationship write.

### R5-M6 — apoc path traversal crosses tenants (graph_analytics_service.py:467-476,722-731,797-805)
apoc.algo.shortestPath/kShortestPaths follow ANY relationship through ANY tenant's nodes; only start/end org-checked. On legacy unscoped/""-partition edges: foreign node ids + path structure returned and persisted into PathAnalytics.paths.

### R5-M7 — delete_relationship scope mismatch vs read (knowledge_graph_service.py:1562-1600 + api/search/knowledge_graph.py:539-556)
No organization_id scope; predicate only `source_document_id IN org_docs`. Entities without source_document_id (ALL arXiv entities) → org user's DELETE on a relationship GET just listed returns 404 forever.

### R5-M8 — /knowledge-graph/analytics undercounts (api/search/knowledge_graph.py:1070-1089)
Passes source_document_ids only, never organization_id; service scopes by e.source_document_id IN. Org entities lacking source_document_id invisible → analytics zeros despite populated graph (contrast /entities org-index-scoped).

### R5-M9 — unbounded collect() before max_results (graph_analytics_service.py:287-294,513-543,555-599,986-1015)
pagerank/triangle/clustering collect EVERY result into one Neo4j record → multi-MB records, Python OOM on 10k-node orgs; max_results truncates after. Pagerank runs org-wide TWICE per request.

## MEDIUM — threads/workspaces

### R5-M10 — /threads/{id}/context corrupted shape + unreachable 404 (threads.py:702-722 + chat_service.py:1066-1079,1121-1132)
Route: `"messages": context` (nests whole dict), `"message_count": len(context)` → always 2. `if not context` never true (service returns populated metadata even for no-access/nonexistent) → unauthorized ids get 200 with empty nested messages instead of 404.

### R5-M11 — feedback PATCH commits before thread-match check (threads.py:870-899)
Service commits feedback (chat_service.py:884-900); route checks message.thread_id != thread_id AFTER → 404 response with write already committed. DELETE route at :919-924 does it in the right order.

### R5-M12 — export ignores soft deletes (export_service.py:441-489)
No Thread.is_deleted or parent soft-delete filters; message loop filters only superseded_by + system-role, not msg.is_deleted. User-deleted messages (incl. scrubbed PII) reappear in every Markdown/PDF/JSON/HTML export.

### R5-M13 — /api/v2/search/health unauthenticated (thread_search.py:465-508, main.py:652-654)
No auth dependency; router mounted bare. Enumerates GIN index DDLs (pg_indexes) + FTS liveness. Schema-internals disclosure.

## MEDIUM — evaluation/research

- **R5-M14** /evaluation/real-time swallows own 503 → 500 (evaluation.py:304-331 — only endpoint in file missing except HTTPException: raise)
- **R5-M15** soft-deleted eval jobs still listed + aggregated forever (evaluation.py:381-383,342-348,771-779 + service — nothing honors is_deleted)
- **R5-M16** comparison averages mix answer_relevancy (higher-better) with hallucination_rate (higher-worse) → meaningless baseline_score/improvement_percentage, direction can invert (rag_evaluation_service.py:716-731)
- **R5-M17** ResearchRun stuck RUNNING on process death — no sweeper touches it; RUNNING run rejects resume (409) → bricked (runs.py:424-426)
- **R5-M18** concurrent SSE double-executes run: PENDING check + RUNNING commit non-atomic; no unique (run_id, step_index) → duplicate steps, double tokens (runs.py:357-363,424; research_step.py:37-38)
- **R5-M19** resume drops accumulated context: engine init from blueprint only; persisted ResearchStep.output never rehydrated → synthesize blind to search/screen results (engine.py:33-39, runs.py:377-387)
- **R5-M20** GeneratedDraft __table_args__ empty despite comment claiming unique constraint → concurrent generations: duplicate versions, two is_current rows, arbitrary winner (generated_draft.py:68-69)
- **R5-M21** GET /analytics/metrics/ no org filter — cross-tenant listing (model has organization_id, unused; sibling list_kpis was explicitly fixed) (analytics/metrics.py:77 vs :366-374)
- **R5-M22** realtime analytics pub/sub global: any user publishes to any channel, cross-org broadcast, /test/subscribe sprays 100 junk metrics/call, no rate limit (analytics/realtime.py:149-235,291-326)
- **R5-M23** trigger_extraction 202-declared but inline sequential LLM loop up to 100 docs → tens-of-minutes request, ingress timeout, retry = duplicate spend (extraction_matrix.py:368-528)
- **R5-M24** LLM-judge failures return hardcoded 0.5/0.3 (and _call_llm returns "0.5" on API error) → total outage produces COMPLETED jobs with fake midline scores (rag_evaluation_service.py:355-455,488)
- **R5-M25** triad task reads metadata keys never written (generated_answer/retrieved_context vs writers' search_time_ms/results_count/search_response) → empty contexts once R5-H7 fixed (evaluation_tasks.py:164-169 vs rag_evaluation_service.py:633-637)

## MEDIUM — infra

- **R5-M26** compose DATABASE_URL interpolation appends literal suffix to any override: `${DATABASE_URL:-postgresql://postgres:postgres@}host.docker.internal:54322/postgres` → override yields `...mydbhost.docker.internal:54322/postgres` (verified live) (docker-compose.development.yml:14,229,304,333)
- **R5-M27** deploy.yml production job unreachable: needs deploy-staging (env-gated, no always()) → prod dispatch green, nothing deployed, rollback dead; staging smoke swallows failure with `|| echo pending` (deploy.yml:59-65,122,130-137)
- **R5-M28** synthetic traffic picks arbitrary REAL org (select limit 1, no order by) — real quota consumed, real users' org-scoped search can surface synth docs; contradicts its own docstring (synthetic_traffic.py:220-264)
- **R5-M29** synthetic ingest dedup death: 6 rotating papers vs org content-hash dedup; cleanup only soft-deletes Collections → after ~14h every paper permanently TOOL-FAILED → alert signal dead (synthetic_traffic.py:92-99,717-793)
- **R5-M30** retention never cleans synthetic Documents/Spaces objects/chunks/memories — unbounded dev growth under 20-min cron (synthetic_traffic.py:20-23; config.py:675-687 covers threads/events only)
- **R5-M31** staging chain absent: no bump writes values-staging; it pins short tag "8ec52eb" but registry only has full-SHA tags → ImagePullBackOff; argocd dir missing staging.yaml/production.yaml — AGENTS.md description doesn't match repo (release-dev.yml:76-92, docker-build.yml:67, values-staging.yaml:26-27, root.yaml:13-17)
- **R5-M32** PDB minAvailable 1 on single-replica backend + celery → zero voluntary disruptions; drains/upgrades hang (values-dev.yaml:447-449, pdb.yaml:29)
- **R5-M33** dev compose binds Flower (unauth API: purge/retry), Redis, Neo4j, Adminer, MinIO on 0.0.0.0 — LAN can purge Celery jobs / control Flower (docker-compose.development.yml:330-340,176-177,207-209,404-421,354-356)
- **R5-M34** values-production as-is: tag latest + IfNotPresent (stale node cache); Neo4j heap 256m/pagecache 128m in 1Gi — the config values-dev documents as guaranteed OOMKill (values-production.yaml:34-35,253-262)

---

## LOW — KG
- **R5-L1** nondeterministic pagination: batch writes share datetime() instant (ORDER BY created_at DESC ties) → dup/missing rows across pages; get_neighborhood RETURN LIMIT without ORDER BY (knowledge_graph_service.py:966-973,1118-1131,1726-1727)
- **R5-L2** start entity consumes LIMIT slot → limit-1 neighbors after Python filter (:1623-1643)
- **R5-L3** API limit le=2000 vs service silent clamp 200 (:1090 vs api :453-455)
- **R5-L4** session._database private attr — brittle; database_size silently None (:2443)
- **R5-L5** located_in pattern matches bare "in"/"at" substrings → LOCATED_IN edges at 0.7 between arbitrary entities; direction never reversed (entity_extraction_service.py:356-376)
- **R5-L6** dead: graph_algorithms instantiated never invoked (handlers return mocks); knowledge_graph_service_improved zero callers; rag_evaluation imports module object unused

## LOW — threads
- **R5-L7** conversation thread_count/message_count include soft-deleted — sidebar badge never drops (conversation_service.py:140-151; conversation.py:98-101)
- **R5-L8** ilike wildcard injection ×2 new (conversation_service.py:110-115, chat_service.py:1142-1153)
- **R5-L9** add_member nonexistent user_id → IntegrityError 500 (workspace_service.py:264-271)
- **R5-L10** deprecated v1 SSE: check-then-add race, per-process guard, wedged id → permanent 409 til restart; str(exc) leak (stream.py:107-131,196,199)
- **R5-L11** no workspace/project quota anywhere (workspace_service.py:57-104)
- **R5-L12** v1 workspace detail lists soft-deleted members once H3 fixed (conversations.py:148,161)

## LOW — evaluation/research
- **R5-L13** EvaluationType()/report_type free strings → 500 / post-200 task failure (evaluation.py:170)
- **R5-L14** BatchEvaluationRequest.queries unbounded; search_limit no bounds (evaluation.py:64-66)
- **R5-L15** get_metrics_summary loads all full metric rows (Text cols) into Python (evaluation.py:771-779)
- **R5-L16** create_task unreferenced → GC-eligible; _extraction_status in-memory → cross-pod 404 + unbounded (extraction_matrix.py:193-200; extraction_matrix_service.py:37; drafts.py:423)
- **R5-L17** citation with neither document_id nor message_id invisible to creator (citations.py:301-313)
- **R5-L18** get_evaluation_metrics ignores organization_id, swallows → [] (rag_evaluation_service.py:903-927)
- **R5-L19** ResearchEvidence has zero writers — evidence tables forever empty; export reads empty table (research_evidence.py; export_service.py:54-77)

## LOW — infra
- **R5-L20** cronjob deadline 600s < worst-case 1440s (360×4 HITL) → killed + backoffLimit 1 re-runs sweep (synthetic-traffic-cronjob.yaml:33-36)
- **R5-L21** synthetic_traffic no ENVIRONMENT refusal (synthetic_traffic.py:526)
- **R5-L22** gitops-image-update image_tag unvalidated → typo → prod crashloop (gitops-image-update.yml:16-17,55)
- **R5-L23** moby/buildkit:latest unpinned (docker-build.yml:96-97)
- **R5-L24** MINIO_ROOT_PASSWORD no default → container exits; dev boot pip-installs unpinned langgraph (compose :359, 88-91)
- **R5-L25** synthetic bootstrap non-healing: crash between user+workspace commits → permanently broken (synthetic_traffic.py:267-303)

---

## Verified NOT buggy (round 5)

- **KG tenant scope (live writers)**: every live create_entity/create_relationship caller passes org; API forces-overwrite client org (search/knowledge_graph.py:201,500,674-680); repair/multimodal/tasks/change_tracker pass it. R2-H1's org=None callers fixed.
- **MERGE/None**: merge keys coalesced to "" (entities:502/538; batch:2041-2069; rels:1256); entity_canonical_org_unique constraint backs race-safety.
- **Cypher injection**: all values bound params; interpolated tokens are enums/clamped ints/validated UUIDs. No document content in Cypher.
- **Traversal bounds**: depth ≤5, typed :RELATED_TO (1622,1693,1853); API enforces.
- **KG transactions**: create_entities_batch + delete_document_graph wrap in begin_transaction.
- **Threads ownership funnel**: workspace_access member/owner/public enforced; nested-route chain validation correct.
- **Supersede integrity**: no chains (UPDATE only NULL rows), no cycles, owner-scoped, atomic UPDATE...RETURNING.
- **Count parity**: thread/message/conversation/collection/workspace lists + FTS share conditions; cursor has_more via limit+1 sentinel.
- **Orphan messages**: by-id fetch 404s under soft-deleted ancestors.
- **Research API tenant scoping** (drafts, citations, extraction, pipeline, report, chat, writer, export, engine): consistent owner/org filters; bibliography format validated; bibtex content-type correct.
- **Infra**: compose/env committed secrets are placeholders; .gitignore/.dockerignore cover .env*; LangSmith project forced from env (no prod misroute); release-dev race handling sound (concurrency cancel + SHA guard); docker-build SHA-pinned checkout, persist-credentials false, no secrets in build args; secret-scan + agent-eval safe interpolation; no pull_request_target anywhere; Dockerfiles.prod non-root + healthchecks; SENTRY token via buildkit secret mount.
- **AGENTS.md drift noted**: qdrant subchart no longer exists (chart clean); staging bump path absent from repo (see R5-M31) — doc vs repo mismatch, not counted as code bug.
