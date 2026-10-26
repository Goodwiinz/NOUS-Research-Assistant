# NOUS RAG System — Bug & Performance Audit Report

> **Historical record (written 2025-05-04, moved here from the repository root on 2026-10-17). Findings and counts are not current status. Check the code before acting on any item, and see the other [audit records](README.md) for later dated evidence.**

**Branch:** `audit/bug-perf-swarm`  
**Worktree:** `.worktrees/audit-bug-perf`  
**Date:** 2025-05-04

---

## Summary

| Domain | 🔴 Critical | 🟡 High | 🟢 Medium | ❓ Low | Total |
|--------|-------------|---------|-----------|--------|-------|
| Backend | 35 | 11 | 0 | 0 | 46 |
| Frontend | 8 | 8 | 5 | 2 | 23 |
| Infrastructure | 18 | 27 | 20 | 7 | 72 |
| **Total** | **61** | **46** | **25** | **9** | **141** |

---

## Backend — Critical Issues (🔴)

### Async/Fire-and-Forget Coroutine Bugs

| File | Line | Issue |
|------|------|-------|
| `api/agent/execute.py` | 279 | `background_tasks.add_task(_run_agent_graph)` — async coroutine passed to sync task runner, never awaited |
| `api/agent/execute.py` | 322 | `background_tasks.add_task(_resume_agent_graph)` — same coroutine-await bug |
| `api/search/search.py` | 231, 312 | `background_tasks.add_task(log_search_query)` — async function, never executed |
| `api/search/search.py` | 1050 | `background_tasks.add_task(persist_api_key_usage_log)` — same |
| `tasks/document_processing_tasks.py` | 355 | `asyncio.run()` passed generator expression instead of coroutine list — **will crash** |

**Fix:** Use `asyncio.create_task()` with stored references, Celery for true background tasks, or make functions synchronous.

### Bare `except: pass` — Silent Failure

| File | Line | Issue |
|------|------|-------|
| `api/documents/document_upload.py` | 200, 215 | WebSocket send errors swallowed |
| `middleware/file_upload_security.py` | 233, 504 | Security validation & malware scan errors hidden |
| `services/evaluation/hhem_faithfulness_service.py` | 161, 173 | Model load & inference failures hidden |
| `services/processing/multimodal_processing_service.py` | 488, 570 | OCR & audio processing errors hidden |
| `tasks/document_processing_tasks.py` | 142 | Upload progress errors swallowed |
| `core/database_optimizations/connection_pool_manager.py` | 510, 543 | DB ping failures hidden |

**Fix:** Replace with `except SpecificException as e: logger.error(...)`

### `asyncio.run()` in Async Context / Celery Tasks

| File | Line | Issue |
|------|------|-------|
| `services/search/vector_search_service.py` | 63, 155, 411 | `asyncio.run()` inside sync methods — event loop overhead & thread safety issues |
| `tasks/document_processing_tasks.py` | 90 | `asyncio.run()` in Celery task — creates/destroys loop per document |
| `tasks/processing_tasks.py` | 145 | `asyncio.new_event_loop()` + `run_until_complete()` — leaks event loops |
| `tasks/summarize_thread_task.py` | 80 | `asyncio.run()` in Celery task |
| `security/security_monitoring.py` | 834 | `asyncio.run()` inside async monitoring loop — nested event loop crash risk |

**Fix:** Make Celery tasks `async` with async worker, or use `asyncio.run()` only at entry points.

### `asyncio.create_task` Fire-and-Forget

| File | Line | Issue |
|------|------|-------|
| `services/documents/document_upload_service.py` | 310 | Job processing task lost, exceptions swallowed |
| `services/infrastructure/realtime_service.py` | 492 | 3 tasks created without references — GC risk |
| `services/research/draft_generation_service.py` | 142 | Draft generation task lost |
| `services/websocket/websocket_manager.py` | 554 | Disconnect task lost |
| `websocket/server.py` | 457 | Redis subscription task lost |

**Fix:** Store task references and add `done_callback` for error handling.

### Memory / Performance

| File | Line | Issue |
|------|------|-------|
| `api/search/search.py` | 446 | `.all()` loads ALL completed documents for reindexing — OOM risk |
| `services/security/encryption_service.py` | 468 | `.all()` loads ALL encrypted profiles |
| `tasks/analytics_processor.py` | 768 | `.all()` loads all active organizations |
| `tasks/processing_tasks.py` | 502 | `.all()` loads all document entities |
| `services/search/hybrid_vector_search_service.py` | 89 | Blocking `requests.post` in sync service consumed by async endpoints |
| `services/knowledge_graph/knowledge_graph_service_improved.py` | 57 | `time.sleep` blocks async event loop during Neo4j retries |

**Fix:** Add pagination/yield_per, use `httpx.AsyncClient`, replace `time.sleep` with `asyncio.sleep`.

### Race Conditions / Global Mutable State

| File | Line | Issue |
|------|------|-------|
| `services/agent/graph.py` | 122 | Global `execute_tool` variable patched concurrently — race condition |
| `services/agent/job_store.py` | 47 | Global `_LAST_L1_CLEANUP` without synchronization |
| `services/connectors/__init__.py` | 27 | Singleton `__new__` not thread-safe |
| `services/knowledge_graph/knowledge_graph_service.py` | 145 | Class-level `_driver_instance` shared without locks |
| `websocket/server.py` | 55 | Global mutable state modified in lifespan |

**Fix:** Use `threading.Lock`, `asyncio.Lock`, or dependency injection.

### Threadpool Blocking

| File | Line | Issue |
|------|------|-------|
| `api/search/search.py` | 172, 253, 336, 430 | `get_db_sync()` in async endpoints blocks threadpool |

**Fix:** Use `get_db()` (async) instead.

---

## Frontend — Issues

### Critical (🔴) — SSR Crashes, Memory Leaks, Stale Closures

| File | Line | Issue |
|------|------|-------|
| `src/components/optimized/OptimizedDocumentList.tsx` | 143 | `window.innerHeight` accessed during **render phase** without `typeof window` guard. **Will crash Next.js SSR.** |
| `src/hooks/usePerformanceMonitoring.ts` | 13 | `performance.now()` called in `useRef()` initializer (runs during render). **Crashes SSR** — `performance` is undefined in Node.js. |
| `src/hooks/useAuth.tsx` | 51 | `contextValue` object recreated **every render** without `useMemo`. All auth consumers re-render on every provider render. |
| `src/hooks/useRealtimeProcessing.ts` | 82 | `useRealtimeProcessingStore()` called **without selector**. Subscribes to entire large Zustand store — every store update triggers re-render. |
| `src/hooks/useWebSocketConnection.ts` | 176 | Heartbeat `setInterval` overwrites `heartbeatTimeoutRef` without clearing previous. Multiple reconnects leak intervals. |
| `src/hooks/useWebSocketConnection.ts` | 294 | Effect cleanup only calls `clearTimeouts()` + `ws.close()` but **never clears heartbeat interval**. `setInterval` leaks on unmount/reconnect. |
| `src/hooks/useSidebarToggle.ts` | 23 | **Stale closure**: `useEffect([])` captures `toggle` function (line 49) but `toggle` is NOT in dependency array. Keyboard shortcut `Ctrl+B` operates on **stale state** — can only toggle once, then stops working. |
| `app/page.tsx` | 122 | `if (!mounted) return null` after `useEffect(() => setMounted(true), [])`. **Hydration mismatch** — server renders `null`, client renders content. |

### High (🟡) — Performance, Leaked Timers, Type Safety

| File | Line | Issue |
|------|------|-------|
| `src/components/optimized/OptimizedDocumentList.tsx` | 106 | `throttle()` created inside `useCallback` without cleanup. Each dependency change leaks a throttle instance with internal pending timers. |
| `src/components/optimized/OptimizedDocumentList.tsx` | 185 | `debounce()` created inside `useMemo` without cleanup. Changing `organizationId` leaks the old debounce timer. |
| `src/hooks/usePerformanceMonitoring.ts` | 56 | `useRenderPerformance` effect **missing dependency array**, runs on every render adding measurement overhead. |
| `src/store/projectStore.ts` | 98 | `catch (error: any)` anti-pattern (repeated **14+ times** in file). Loses type safety; should use `unknown` with type guard. |
| `src/components/search/SearchInterface.tsx` | 138 | `onGetHistory().then(setHistory)` only catches with `console.error` — **no UI error state handling** on fetch failure. |
| `src/hooks/useKeyboardShortcuts.ts` | 46 | `shortcuts` array in `useCallback` deps. If parent passes new array reference each render, keyboard listeners re-register constantly. |
| `src/components/graph/ResponsiveGraphLayout.tsx` | 184 | `window.innerWidth` used for breakpoint logic **instead of container ref dimensions**. Mismatches `ResizeObserver`, causes layout jitter. |
| `src/components/layout/dashboard/DashboardLayout.tsx` | 47 | `useEffect` depends on `sidebarState` and calls `setSidebarState` inside resize handler. Causes **double execution** on every resize. |

### Medium (🟢) — Code Quality, Config

| File | Line | Issue |
|------|------|-------|
| `components/**/*.tsx` | — | **123 instances** of `.map((item, index) => <... key={index}>)`. Using array index as React key causes reconciliation bugs on list reorder. |
| `next.config.js` | 39 | `images.remotePatterns` only allows `localhost`. Production images from external domains **won't be optimized** by Next.js. |
| `next.config.js` | 19 | `typescript.ignoreBuildErrors: true` in non-production builds masks TypeScript errors in CI/dev. |
| `src/components/ui/sidebar.tsx` | 101 | `document.cookie = ...` assignment inside `setOpen` callback is a **side effect during state transition**. |
| `app/page.tsx` | 49 | `FeatureCard` props typed as `any`. Weakens type safety in landing page. |

### Low (❓) — Needs Review

| File | Line | Issue |
|------|------|-------|
| `src/services/streamingService.ts` | 1 | **Deprecated file** still present in codebase. May still be imported, adding bundle bloat. Verify no imports remain. |
| `src/components/optimized/OptimizedDocumentList.tsx` | 256 | WebSocket `handleStatusUpdate` uses `any` type for message payload. Should define a strict interface. |

---

## Infrastructure — Critical Issues (🔴)

### Security

| File | Line | Issue |
|------|------|-------|
| `config/docker-compose/docker-compose.prod.yml` | 90 | `CORS_ORIGINS="*"` in production |
| `config/docker-compose/docker-compose.security.yml` | 22 | `CORS_ORIGINS=*` wildcard in security testing |
| `backend/src/services/knowledge_graph/*.py` | 109, 124, 142 | `allow_origins=["*"]` with `allow_credentials=True` — CSRF risk |
| `config/docker-compose/docker-compose.prod.yml` | 469, 489 | Traefik `--api.insecure=true` + dashboard port 8080 exposed |

### Hardcoded Credentials

| File | Line | Issue |
|------|------|-------|
| `config/docker-compose/docker-compose.development.yml` | 379, 380 | `MINIO_ROOT_USER=minioadmin` / `MINIO_ROOT_PASSWORD=minioadmin123` |
| `config/docker-compose/docker-compose.services.yml` | 291 | `POSTGRES_PASSWORD=postgres` |
| `config/docker-compose/docker-compose.services.yml` | 330 | `NEO4J_AUTH=neo4j/neo4jpassword` |
| `config/docker-compose/docker-compose.services.yml` | 417 | `GF_SECURITY_ADMIN_PASSWORD=admin` |
| `config/docker-compose/docker-compose.websocket.yml` | 336 | `GF_SECURITY_ADMIN_PASSWORD=admin123` |
| `config/docker-compose/docker-compose.azure.yml` | 145 | `NEO4J_AUTH=neo4j/neo4jpassword` |
| `config/docker-compose/docker-compose.websocket.yml` | 48 | `NEO4J_PASSWORD=neo4jpassword` |
| `config/docker-compose/docker-compose.security.yml` | 18 | `SECRET_KEY=security-test-secret-key` |

### Vulnerable Dependencies

| File | Package | Issue |
|------|---------|-------|
| `backend/requirements.txt` | `fastapi==0.104.1` | CVE-2024-24762, CVE-2024-32982 — upgrade to >=0.115.0 |
| `backend/requirements.txt` | `pillow==10.3.0` | CVE-2024-28219 — upgrade to >=10.4.0 |
| `backend/requirements.txt` | `opencv-python==4.8.1.78` | Known issues — upgrade to >=4.10.0 |
| `backend/requirements.txt` | `anthropic==0.8.1` | Very outdated — upgrade to >=0.40.0 |
| `backend/requirements.txt` | `qdrant-client==1.16.2` | May have known issues |

### CI/CD Issues

| File | Line | Issue |
|------|------|-------|
| `.github/workflows/fast-ci.yml` | 23, 54 | `continue-on-error: true` masks lint & test failures |
| `.github/workflows/docker-build.yml` | 103 | Trivy vulnerability scan disabled |
| `.github/workflows/trigger-deploy.yml` | 29 | Infisical action not pinned by SHA |

---

## Recommended Priority Order

### P0 — Fix Immediately (Production Risk)
1. **CORS `*` in production** — CSRF / credential theft risk
2. **Hardcoded passwords** — credential exposure in compose files
3. **Traefik insecure dashboard** — admin interface exposed
4. **`asyncio.run()` in Celery tasks** — event loop crashes under load
5. **`background_tasks.add_task` with async** — background jobs silently fail
6. **Frontend SSR crashes** — `window.innerHeight` (OptimizedDocumentList:143), `performance.now()` (usePerformanceMonitoring:13), hydration mismatch (page.tsx:122)
7. **WebSocket heartbeat leak** — `useWebSocketConnection.ts` interval ref overwritten, never cleared on unmount/reconnect
8. **Stale closure in sidebar toggle** — `Ctrl+B` keyboard shortcut stops working after first toggle
9. **Zustand store without selectors** — `useRealtimeProcessing.ts:82` subscribes to entire store, excessive re-renders
10. **`dangerouslySetInnerHTML`** — XSS vectors in frontend

### P1 — Fix This Sprint
7. All `bare except: pass` — silent failures hide production issues
8. `asyncio.create_task` fire-and-forget — unhandled exceptions & GC
9. `.all()` without pagination — OOM on large datasets
10. Outdated dependencies with CVEs
11. `continue-on-error: true` in CI — broken tests merged
12. **Frontend leaked timers** — throttle/debounce in OptimizedDocumentList recreated without cleanup
13. **`catch (error: any)` pattern** — 14+ instances in projectStore.ts, loses type safety
14. **Auth context missing `useMemo`** — all consumers re-render on every provider render
15. **Missing `next/image` remotePatterns** — production images won't be optimized

### P2 — Fix Next Sprint
13. Global mutable state / race conditions
14. Blocking sync DB calls in async endpoints
15. `time.sleep` / `requests.post` in async paths
16. Docker security contexts (read_only, cap_drop)
17. Ports published to host without binding

---

*Generated by audit swarm agents (cavecrew pattern) + manual verification.*
