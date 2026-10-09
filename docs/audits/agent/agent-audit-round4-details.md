# Agent audit round 4 — detailed findings — 2026-08-18
Source: opencode session, 4 explore agents (documents/files API, auth/middleware/ws core, frontend upload+ws, fresh merges)
Status ledger: ~/.audit-ledgers/rag/agent-audit-round4.md

---

## HIGH — upload + realtime pipeline broken end-to-end

### R4-H1 — WS subprotocol mismatch: frontend ['auth', token] vs backend access_token.<jwt> (websocket.ts:42 vs websocket.py:66-79)
Frontend sends `new WebSocket(url, ['auth', token])`; backend v1 `/ws` (mounted at root) parses only `access_token.<jwt>` from Sec-WebSocket-Protocol → finds none → closes 1008 "Missing authentication token" → frontend reconnect loop (2/4/8/16/32s) → gives up after 5. Every `useWebSocket()` consumer (incl. ProcessingStatus.tsx realtime mode on the live documents page) permanently shows disconnected; zero realtime updates ever arrive. useWebSocketConnection.ts:217 and realtimeWebSocketService.ts:129 repeat the same `['auth', token]` convention — only `/api/v2/ws/connect` understands it, and nothing connects there. VERIFIED in code.

### R4-H2 — emit(type, payload) but consumers deref data.payload.* (websocket.ts:57 + useWebSocket.ts:175, useDocumentProcessingStatus.ts:86, useDocumentUpload.ts:107)
`emit(message.type, message.payload)` invokes handlers with the payload directly; every handler reads `data.payload.job_id` / `payload.document_id` → `undefined` deref → TypeError swallowed by emit's try/catch (websocket.ts:167-173) with console error only. Even with a working connection, processing updates crash in every handler and never reach state.

### R4-H3 — /ws never sends document_processing_update (websocket.py:104-158)
v1 endpoint sends only `connected` + `pong`; updates marked "future". Entire frontend pipeline (useDocumentProcessingUpdates, useQueryStatusUpdates, useSystemNotifications, WS progress in useDocumentUpload.ts:101-134) consumes an event no server produces. Realtime progress silently never works, compounding H1/H2.

### R4-H4 — upload omits required title; response shape mismatch (uploadService.ts:320-341 vs files.py:135, 98-112)
Backend `POST /files/upload` requires `title: str = Form(...)`; api.upload() sends only the file → 422 on every upload. Even if passed: response mapping reads `response.job_id` and `response.file_info.id` — FileUploadResponse has neither → poll `/processing/jobs/undefined` → 404 → 600×2s retry → "Processing timeout" after 20 min. Live consumers: DocumentUploadWizard.tsx:87, ProcessingDashboard.tsx:88. VERIFIED in code.

### R4-H5 — default allowedTypes are extensions; validator matches MIME (useDocumentUpload.ts:56 + fileValidation.ts:97-99)
Default `allowedTypes` = `UPLOAD_LIMITS.SUPPORTED_FORMATS` = extensions ['pdf','txt','jpg',…]; `validateFileType` checks `file.type` (MIME) or `type.includes('.ext')`. `'image/jpeg'.includes('jpg')` false; `'image/jpeg'.includes('.jpg')` false → every file fails UNSUPPORTED_TYPE at addToQueue when defaults used (wizard passes no allowedTypes; ProcessingDashboard passes no options). "Start Upload" rejects 100% of files. Wizard's own onDrop passes MIME keys so the failure surfaces one step later — more confusing.

### R4-H6 — scheduleReconnect never reconnects (realtimeWebSocketService.ts:405-418)
Timer callback only sets status 'reconnecting' (comment admits fresh token needed, "handled by calling code" — nothing handles it). Abnormal close (blip, restart) permanently kills the singleton; dashboards show stale "reconnecting" forever. Manual retryConnection works (connect() early-returns only when OPEN) — auto-recovery does not exist.

### R4-H7 — pong never clears heartbeat timeout (useWebSocketConnection.ts:141-147, 179-184)
sendPing arms heartbeatTimeoutRef (ping+30s, closes socket); 'pong' handler updates latency, never clearTimeout. Every healthy connection force-closed ~30-60s after connect ("Heartbeat timeout"), reconnect churn forever. Latent: WebSocketProvider never mounted, but hook exported and half-wired into OptimizedDocumentList.

---

## MEDIUM — backend documents/files/realtime

### R4-M1 — naive/aware datetime TypeError kills live-status endpoint (realtime_document_status.py:357-358)
`datetime.utcnow()` (naive) minus `processing_started_at` (tz-aware; column DateTime(timezone=True), asyncpg returns aware) → TypeError → 500 on GET /api/v2/realtime/documents/{id}/status for any PROCESSING doc with 0<progress<100 — exactly the doc a user watches mid-processing. documents.py:851 does it right (now(timezone.utc)).

### R4-M2 — no UUID validation on path params (files.py:333,357,425,483,520,548,706 + processing.py:68,97)
`Document.id == "abc"` → asyncpg DataError → unhandled 500 + stack logs. documents.py added validate_uuid for this exact failure; files.py/processing.py never got it. Scanner-friendly 500 farm.

### R4-M3 — unbounded/unsigned limit+offset on job listing (processing.py:150-158)
`limit: int = 50, offset: int = 0`, no ge/le. `?limit=1000000` dumps org job table incl. parameters (file_path, mime) and result JSON per row; negative → DB error → 500.

### R4-M4 — batch processing unbounded (processing.py:58-59, 217-261)
BatchProcessingRequest.document_ids no max_length (bulk-delete caps 100). 10k ids → 10k sequential process_document calls, each with_for_update row lock, one request → minutes-long request, worker flood, lock contention.

### R4-M5 — unauthenticated status/metrics endpoints (websocket.py:167-175, websocket_v2.py:394,640,659)
/ws/status, /api/v2/ws/status, /api/v2/ws/channels, /api/v2/ws/health — no auth dep; MultiTenancyMiddleware only skips tenant context, doesn't reject. Anonymous recon: connection counts, active users, per-channel subscribers, system load, job stats, error rate.

### R4-M6 — cancel_upload job branch never revokes Celery task (files.py:619-654)
Marks job CANCELLED, commits, "Upload cancelled successfully" — no control.revoke(celery_task_id) (processing.py:374-377 revokes properly). Running ingestion continues → doc lands COMPLETED with chunks after user "cancelled".

## MEDIUM — auth/ws core

### R4-M7 — WS auth skips CLI-token revocation (websocket_auth.py:125 + websocket_v2.py:216)
authenticate() calls verify_token only; is_cli_token_revoked lives solely in get_current_user (dependencies.py:25-35). Revoked CLI token (up to 30d life) still opens /api/v2/ws/connect and receives org pushes until natural exp; heartbeat monitor enforces expiry, not revocation.

### R4-M8 — WS admin gate trusts JWT role claim (websocket_v2.py:276,301-304 + websocket_manager.py:778-782)
Admin channels gated on token app_metadata.role; multi_tenancy.py:139-142 explicitly says DB is source of truth. Demoted admin keeps admin-channel subscription via existing CLI token up to 30d. Org IS resolved from DB; role is not.

### R4-M9 — current_user.is_superuser doesn't exist (websocket_v2.py:478,604)
User model has no is_superuser/is_super_admin/is_locked (grep-verified). Admin inspecting another user's connection → AttributeError → 500. Admin escape hatch broken; fail-closed by accident.

### R4-M10 — rate_limiting Redis error path UnboundLocalError (rate_limiting.py:93-127)
`info` first assigned line 102, after zremrangebyscore/zcard; `except RedisError: return True, info` (124-127) → first-call failure → info unbound → UnboundLocalError → 500 on every analytics-path request during Redis outage (intended fail-open). Also: pipeline created never used (non-atomic RMW), sync redis client blocks event loop in async dispatch.

### R4-M11 — JWKS cached forever (security.py:33,140-154)
Global _supabase_jwks_cache, fetched once, no TTL/invalidation. Supabase rotates signing keys → all ES256 verification fails → all ES256 users 401 until pod restart. Self-inflicted auth outage on rotation.

### R4-M12 — dev/staging 500 bodies leak exception details (main.py:765-783 + websocket_v2.py:375, websocket_manager.py:830)
`if settings.ENVIRONMENT not in ("production",)` — dev + staging are live internet-reached (dev-api.gen-text.app) and ship raw `type(exc).__name__: {exc}` (SQL fragments, paths, upstream errors) to any client.

### R4-M13 — unconditional XFF trust (security.py:59-93)
`_install_proxy_aware_client_patch` rewrites HTTPConnection.client process-wide whenever X-Forwarded-For present — no trusted-proxy config. Safe only behind single ingress; any direct reachability (NodePort, port-forward, misconfigured ingress passing client XFF) forges request.client.host → bypasses every IP-keyed control (auth_rate_limiter 50/15min, analytics fallback, WS auth limiter, audit IPs). api_security.py:195 takes [-1] while its comment says "first in list".

### R4-M14 — /workers/status|health under-authorized (workers.py:47-48,318-319)
Only get_current_user (any USER). Worker hostnames, pool concurrency, queue names, processed counts to every tenant user; error paths put str(e) in detail. USER-triggerable inspect() fan-out storm. Siblings (/queues,/ping,/shutdown) correctly require_admin.

## MEDIUM — frontend upload/docs

### R4-M15 — shared-socket ownership war (useRealtimeProcessing.ts:326-345 + realtimeWebSocketService.ts:204-221)
Every consumer (ConnectionManager, PerformanceMonitor, NotificationCenter ×2, RealtimeStatusDashboard) calls connect() on mount, disconnect() on unmount against the SAME singleton → unmounting any one kills socket for all. connect() re-runs subscribe() ignoring returned unsubscribe → duplicate handlers per reconnect → duplicate notifications.

### R4-M16 — 2min poll budget vs service's own >2min estimate (enhancedDocumentService.ts:154,530-539)
60×2s then "Timed out waiting…" — live upload page marks file failed while server keeps processing (estimateProcessingTime itself says video >2min). Later indexes; re-upload hits content-hash dedup → "Already uploaded" dead end. UPLOAD_PROCESSING_TIMEOUT_MS (5min) exists unused.

### R4-M17 — preview-cleanup revokes live object URLs (DocumentUploader.tsx:52-60, DocumentUploadWizard.tsx:311-319)
Effect keyed on selectedFiles runs with OLD array on every change; revokes siblings' still-in-use preview URLs → thumbnails break after second interaction.

### R4-M18 — failed queue items never cleaned (uploadService.ts:426-435 + 346-348, 384-388)
cleanupCompleted requires completedAt; error paths never set it → failed items immune to 30-min cleanup and manual cleanupCompleted(0) → unbounded session queue/stats.

### R4-M19 — AbortController never wired (uploadService.ts:316-317, DocumentUploadZone.tsx:419-420, BatchUploadManager.tsx:160-161)
Controllers created, never passed; api-client XHR path has no abort support at all. Cancel/navigate mid-upload → upload completes server-side anyway (quota, doc created).

### R4-M20 — optimistic rollback overwrites refetched list; name-collision ids (useOptimisticUpload.ts:83-94, 24, 43)
onSettled invalidates+refetches (correct), then 5s setTimeout restores previousDocuments over the fresh list. `id: file.name` → two same-name files share optimistic id, progress mutates both.

### R4-M21 — dead endpoints (documentService.ts:21,78)
POST /documents/upload + /documents/batch-upload don't exist in v1 API (real: /files/upload). documentService.uploadFile/uploadFiles 404; useOptimisticUpload built on them.

### R4-M22 — batch delete refetch races (useDocuments.ts:477-497, DocumentLibrary.tsx:189-195)
Promise.allSettled of deletes each triggering fetchDocuments → N concurrent refetches interleaved with in-flight deletes; last fetch may predate final commit → deleted docs reappear until manual refresh. Library variant: N+1 sequential refetch flicker.

### R4-M23 — rows never transition processing→indexed (DocumentCard.tsx:315-326)
No realtime (broken per H1-H3), no polling in useDocuments. Upload page → library: spinner "Processing…" indefinitely until manual Refresh.

### R4-M24 — size/type parity drift (types/constants.ts:3 vs organization.py:124-131, file_service.py:227-259)
FE hardcodes 50MB; BE rejects above org.max_file_size_bytes (10MB FREE) → full upload then raw-bytes 400. FE allowlist far narrower than BE 31 extensions → backend-supported files rejected client-side.

### R4-M25 — disconnect()/reconnect clears ALL listeners (websocket.ts:105-112, 219-228)
Any reconnect or updateConnectionParams wipes every subscription registered by other components; keyed-on-manager registrations never re-registered → all update channels silently die after token-refresh reconnect.

## MEDIUM — fresh merges

### R4-M26 — Stop during SSE confirm re-arms dead card (agentChatStore.ts:984-993 vs streaming.py:3098-3129)
stopGeneration aborts confirm controller, no server call. streamConfirm swallows AbortError → abort-restore path re-inserts pendingConfirmations[threadId]. Backend saw disconnect → cleanup_cancelled_confirm_response finalized run CANCELLED (terminal). Every later Approve: "Run is not awaiting confirmation" error — exact dead-card shape #1470's fix refused to create. /chat handleStop does it right (cancelPendingConfirmation + clear); only global widget store affected.

---

## LOW — backend

- **R4-L1** HTTPException(400) swallowed by own except → 500 (processing.py:317-325)
- **R4-L2** bulk-delete duplicate ids double-counted in success report (documents.py:1172)
- **R4-L3** unknown date_range silently ignored — no filter, no 400 (documents.py:1044-1055)
- **R4-L4** to_dict leaks storage_path, storage_backend, checksum_sha256, do_kb uuid (files.py:354,285) — recon value only
- **R4-L5** integrity route: sync RoBERTa in request, no rate limit; select-then-insert race → MultipleResultsFound 500 (integrity.py:31-89)
- **R4-L6** bulk status unbounded + include_jobs N+1 (realtime_document_status.py:431-505)
- **R4-L7** sync db.query inside async handlers throughout processing.py (107,135,161,302,341,408)
- **R4-L8** v1 WS manager: no per-user cap, no reaper (websocket.py:17-43)
- **R4-L9** document_management.py dead service: zero auth (client org), file_hash/ProcStatus wrong attrs, quota drift on delete, Content-Disposition injection — delete or quarantine
- **R4-L10** orgless user → unscoped processing lookup (processing.py:79) — use get_current_organization, fail closed
- **R4-L11** auth/api-key rate limiters + CLI revocation fail open on Redis outage (core/rate_limit.py:184, api_key_auth.py:182, config.py:306) — aggregate hardening gap
- **R4-L12** get_current_user_optional can never return None (HTTPBearer auto_error) (dependencies.py:268-290) — latent, zero callers
- **R4-L13** analytics_auth sync db.query on AsyncSession (analytics_auth.py:118-126) — latent, zero callers
- **R4-L14** services/websocket/auth.py nonexistent User attrs (is_locked, is_super_admin) — module unusable, dead (226,467,561)
- **R4-L15** realtime_service.py standalone WS: token in URL + connect() arg mismatch — dead, violates token convention (508-517)
- **R4-L16** defaultdict(set) registries + rate dicts never delete keys — lifetime creep (websocket_manager.py:176-178,551-558, rate_limiting.py:33, core/rate_limit.py:48)
- **R4-L17** v1 /ws never echoes subprotocol — browser handshake fails outright (websocket.py:102)
- **R4-L18** is_public gate dead code: has_permission(USER) true for every role (dependencies.py:258)

## LOW — frontend fresh merges

- **R4-L19** SSE throw after confirmation frame → durable fallback re-runs parked turn; parked card replaced by fallback answer (agentChatStore.ts:469-482,497-508)
- **R4-L20** Stop/supersede relabels interrupted tools as 'failed' — cosmetic (agentChatStore.ts:78-89,1249,716-723)
- **R4-L21** one-shot reattach deletes retry flag before attempt — failed reattach strands thread until remount (useChatStreaming.ts:1472-1474; documented one-shot, noting gap)

## LOW — frontend upload/docs

- **R4-L22** updateFilters/updatePage read state inside setState updater — batched updates fetch with defaults (useDocuments.ts:346-380)
- **R4-L23** search fetch per keystroke, no debounce (DocumentLibrary.tsx:98-104)
- **R4-L24** BatchUploadManager fully simulated + interval leak on unmount; unused (102-131,158-179,235)
- **R4-L25** dead buttons: "Upload N Files" console.log only (DocumentUploader.tsx:310-320); Retry Upload no onClick (UploadProgress.tsx:219-223)
- **R4-L26** poll never cancels after removeFromQueue; cancelled/retrying statuses unhandled → 20-min timeout error (uploadService.ts:372-399)
- **R4-L27** OptimizedDocumentList: useWebSocketConnection() no url → bogus permanent error; unused (OptimizedDocumentList.tsx:207)
- **R4-L28** WS-path status mapping: pending/running/completed → "Unknown status"/eternal spinner (useDocumentProcessingStatus.ts:117, ProcessingStatus.tsx:316)
- **R4-L29** reconnect timer uncancellable post-logout (reconnect with stale token); token refresh never propagates to socket (websocket.ts:179-198, useWebSocket.ts:140-151)

---

## Verified NOT buggy (round 4)

- **Documents API tenant scope**: org filter + is_deleted on list/get/delete/download; presign keys from DB row post-ownership check; collections attach org-scoped; WS delivery org-gated.
- **Count/result parity**: documents.py shared conditions; files.py shared _list_files_filters — no drift.
- **Sort**: enum-only, no injection.
- **Delete ordering**: soft-delete + quota revert commit first, object delete after — compensating direction correct.
- **Status mapping**: all read paths via ApiDocumentStatus.from_db/to_db; 400 on unknown filter input.
- **Reprocess**: flush → enqueue_after_commit → commit — no PENDING stranding.
- **CORS**: explicit allowlist, no wildcards, regex validated, https-only prod/staging.
- **verify_token**: per-path key+algorithm pinning (no alg confusion), aud enforced, expiry enforced, CLI max-age clamp, signup-metadata whitelist.
- **WS v2 connect**: Origin CSWSH gate, org from DB fail-closed, per-user caps, tenant gate on delivery, expiry enforcement, Redis fan-out dedup with real instance_id.
- **Error handlers**: prod 500s sanitized, log-forging neutralized.
- **multi_tenancy**: org/role from live DB row, fail-closed, inactive-org 403.
- **APISecurityMiddleware/AuditMiddleware**: NOT registered — latent landmines dormant.
- **plan_reasoning (#1465)**: single writer planner_node (capped 2000); emitted+persisted on all 4 paths (SSE main/confirm, durable run/resume); reset per turn; PLAN precedes first token; regenerate re-plans + tombstones; migration catalog-only, linear chain; presenter null-safe; frontend truthy-guarded.
- **Stream ownership (#1469)**: monotonic streamOwnerRef; sequential turns can't overlap (submitLock + storeIsStreaming held through finally); only reachable overlap (paused→confirm) fully owner-guarded; mid-function wipes run in sync blocks before any await.
- **Lifecycle (#1470)**: finishRun idempotent, keyed to owning thread; confirm finalizes exact claimed job; finalize UPDATE guarded against terminal overwrite (RunAlreadyTerminalError absorbed).
- **Submission linkage**: run+user+event+outbox in ONE transaction; assistant linkage after persist commit.
- **#1466/#1471 finalize retry**: gated on connection_invalidated only, single retry, terminal propagation preserved.
