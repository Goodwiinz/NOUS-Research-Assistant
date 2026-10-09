# Backend/API Audit Fix Plan (audit backend-api-audit-20260925-150755-68786)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the 27 findings from the backend/API audit (ledger: `~/.audit-ledgers/rag_system/backend-api-audit.md`, SHA `d41ce656132ca5da3deb6a241ebe2cd266086f05`), in dependency order, as small reviewed PRs.

**Architecture:** 4 waves. Wave 1 = auth bypass + rate-limit core (independent PRs, can run parallel). Wave 2 = authz correctness (tenants, WS, API keys). Wave 3 = hygiene batch PRs for LOWs + dead-code deletion. Wave 4 = live-env config verification (authorization-gated). Every PR claims its ledger IDs before branching and sets `Status=pr` on open.

**Tech Stack:** FastAPI, pydantic-settings, SQLAlchemy async, redis / redis.asyncio, python-jose, pytest (`backend/Makefile`).

**Repo conventions:** branch `fix/<ID>-<slug>` off `origin/develop`. Squash-merge. Ledger is source of truth — read it before starting any task.

**Verification command for every task:**

```sh
cd backend && pytest tests/unit -m unit -q --no-cov -k <relevant-module>
# before PR open: full unit suite
cd backend && pytest tests/unit -m unit -q
```

---

## Wave 1 — critical/high

### Task 1: I1 + I24 — fail-closed JWT secret + robust environment gating

Ledger IDs: I1, I24. One PR: `fix/I1-jwt-secret-fail-closed`.

**Files:**
- Modify: `backend/src/core/config.py:15-16, 340-409` (validators), `:73` (ENVIRONMENT default)
- Create: `backend/tests/unit/core/test_config_secret_guards.py`

**Design:**

1. Replace exact-string guard `if env in ("production", "staging")` with a strict-allowlist: strict unless `env.lower() in THROWAWAY_ENVIRONMENTS` where `THROWAWAY_ENVIRONMENTS = MEMORY_FALLBACK_ENVIRONMENTS` (`{"development","testing","local","test","ci"}`; `config.py:27-29`). Any other spelling (`prod`, `prod-eu`, `Production`, `dev-shared`) = strict → no fallback secrets.
2. Add helper next to the constants:

```python
def _is_strict_environment(env: Optional[str]) -> bool:
    value = (env or "development").strip().lower()
    return value not in MEMORY_FALLBACK_ENVIRONMENTS
```

3. In `validate_jwt_secret_key` / `validate_secret_key` / `validate_neo4j_password`: when weak value and `_is_strict_environment(env)` → raise (same messages). Only throwaway envs get `_LOCAL_*` fallbacks. Keep the 32-char minimum for explicitly provided values.
4. JWT_SECRET_KEY: additionally, in strict envs an *unset* value must raise (it already does via weak-pattern `not v` → raise). Verify with test — no silent `_LOCAL_JWT_SECRET_KEY` escape.
5. Add startup log (info) in strict envs: "strict environment: no fallback secrets permitted".

**Tests (write first, watch fail):**

```python
# backend/tests/unit/core/test_config_secret_guards.py
import pytest
from pydantic import ValidationError

def _cfg(**kw):
    from src.core.config import Settings
    return Settings(ENVIRONMENT=kw.pop("env"), **kw)

@pytest.mark.parametrize("env", ["production", "staging", "prod", "prod-eu", "Production", "dev-shared", ""])
def test_strict_envs_reject_weak_jwt_secret(env):
    with pytest.raises(ValidationError):
        _cfg(env=env, JWT_SECRET_KEY="your-secret-key")

@pytest.mark.parametrize("env", ["development", "testing", "local", "test", "ci"])
def test_throwaway_envs_get_local_fallback(env):
    cfg = _cfg(env=env, JWT_SECRET_KEY="your-secret-key")
    assert cfg.JWT_SECRET_KEY != "your-secret-key"

def test_strict_env_rejects_weak_neo4j_password():
    with pytest.raises(ValidationError):
        _cfg(env="prod", NEO4J_PASSWORD="neo4jpassword")
```

**Steps:** failing tests → run (`-k test_config_secret_guards`) → implement → pass → full unit suite → commit `fix(security): fail-closed secret guards for non-throwaway environments (audit I1,I24)` → push, open PR, ledger rows I1,I24 → `pr`.

---

### Task 2: I2 + I3 — global async rate limiting, no fail-open silent pass

Ledger IDs: I2, I3. One PR: `fix/I2-global-async-rate-limit`.

**Files:**
- Modify: `backend/src/middleware/rate_limiting.py` (rewrite `AnalyticsRateLimitMiddleware` → `ApiRateLimitMiddleware`), `backend/src/main.py:499-500` (mount name/scope)
- Modify: `backend/src/api/evidence/router.py:53-60,110-116`, `backend/src/api/threads/threads.py:99-102` (swap sync redis → `redis.asyncio`)
- Reuse (do not duplicate): `backend/src/core/rate_limit.py` — already async, Lua-atomic, per-process fallback (audit R7-M7). Prefer wrapping this over new code.
- Create: `backend/tests/unit/middleware/test_api_rate_limit.py`

**Design:**

1. New dispatch behavior in `ApiRateLimitMiddleware`:
   - Skip: non-`/api/` paths, `/api/v1/auth/*` (auth limiter already covers; avoid double-count), health.
   - Default bucket: per-user (from `request.state.user_id`, set by MultiTenancyMiddleware) else per-IP; e.g. 600 req/5min USER tier, role-multiplied.
   - Heavy buckets by prefix, from a table at top of file:
     `/api/v1/search` 60/min, `/api/v1/research` chat+`/agent` 60/min, `/api/v1/documents` POST (upload) 30/min, `/api/v1/arxiv` bulk 10/min, `/api/v1/connectors` 60/min, `analytics` keep existing role tiers.
2. Use `redis.asyncio.Redis` (or `core/rate_limit.py` limiter instances). Remove all sync `z*` calls from async paths.
3. Failure mode: env flag `RATE_LIMIT_FAIL_CLOSED` (default `false`), but on Redis error log at ERROR with metric and use per-process in-memory limiter for that request instead of silently allowing — never hard-fail all traffic unless flag set.
4. Keep `AnalyticsRateLimitMiddleware` name as thin alias if tests import it; else update imports + `main.py:500`.

**Tests:** search path limited (429 after N), auth path skipped, Redis-down → in-memory fallback still counts, sync-client regression guard (assert no `redis.Redis` sync client constructed in middleware).

**Steps:** tests fail → rewrite → pass → full unit → commit `fix(security): async global rate limiting with heavy-endpoint buckets (audit I2,I3)` → PR → ledger I2,I3 `pr`.

---

### Task 3: I4 — delete hardcoded admin password reset script

Ledger ID: I4. PR: `fix/I4-remove-admin-reset-script`.

**Files:**
- Delete: `backend/scripts/maintenance/reset_admin_password.py`
- Check: no consumer (`grep -r reset_admin_password backend/ docs/` — expect none)

**Steps:** delete → `pytest tests/unit -m unit -q` (sanity) → commit `fix(security): remove hardcoded-credential admin reset script (audit I4)` → PR → ledger I4 `pr`.

If a maintenance path is genuinely needed: separate follow-up that takes password via stdin/env, never prints — out of scope here (YAGNI).

---

## Wave 2 — medium authz correctness

### Task 4: I7 — multi-tenancy fail-closed + exact skip list

Ledger IDs: I7 (+ the `startswith` skip-list LOW folded into it). PR: `fix/I7-tenancy-fail-closed`.

**Files:**
- Modify: `backend/src/middleware/multi_tenancy.py:73-74` (fail-closed), `:112-121` (skip list), exception detail leak at `:237-243`
- Create: `backend/tests/unit/middleware/test_multi_tenancy_middleware.py` (extend existing if present)

**Design:**

1. Skip list: replace `startswith` with exact segment prefixes via regex anchors: `^/health($|/)`, `^/docs$`, `^/redoc$`, `^/openapi.json$`, `^/api/v1/cli-auth/`, `^/api/v1/auth/`. Nothing else passes without tenant resolution.
2. Fail-closed: after auth-skip checks, if no valid Bearer → tenant context unset → `return await call_next(request)` becomes: still pass through ONLY for skipped paths; otherwise let downstream 401 happen naturally? No — the gap: valid-looking public endpoints. Decision: if path not skipped and `Authorization` missing → `JSONResponse(401)` directly in middleware. Invalid token → pass through (dependencies re-verify and 401; avoids double JWT decode cost).
   - Never 403 with internals: `details={"error": str(e)}` at `:237-243` → log full, return `details={"organization_id": organization_id}` only.
3. Add org-membership assertion comment/docs note: middleware does NOT add RLS; per-query convention stays — cross-tenant query sweep is follow-up audit, not this PR.

**Tests:** `/api/v1/documents` without token → 401 from middleware; `/health` + `/api/v1/cli-auth/start` pass without token; `/docsX` does NOT match skip; bad-token on protected path → 401 from dependency; 403 body contains no exception text.

---

### Task 5: I8 — stop leaking `str(e)` to clients

Ledger ID: I8. PR: `fix/I8-no-raw-errors-to-client`.

**Files:**
- Modify: `backend/src/api/realtime/realtime_document_status.py:221-226,425-430,538-543,660-665,712-717`, `backend/src/api/realtime/websocket_v2.py:410` (already DEBUG-gated — verify only), `backend/src/api/threads/thread_search.py:82`, `backend/src/health/endpoints.py:76-77` (I17 partial — genericize detail)
- Test: extend nearest existing API unit tests per module.

**Pattern:**

```python
except Exception:
    logger.error("subscribe failed", exc_info=True)
    raise HTTPException(status_code=503, detail="Failed to subscribe to document updates")
```

Mechanical rule: no `str(e)`/`{e}` inside any `detail=`. `grep -n "detail=.*str(e)\|detail=.*{e}" backend/src/api` must return zero after PR (add grep check to test if cheap).

**Tests:** force exception (mock) → response body has no exception text, status preserved.

---

### Task 6: I5 — enforce API-key `allowed_endpoints`

Ledger ID: I5. PR: `fix/I5-enforce-api-key-scopes`.

**Files:**
- Modify: `backend/src/core/api_key_auth.py` (`get_api_key_data` / calling dependency — add `Request` param, enforce at `:199-296` validation flow)
- Modify: `backend/src/api/auth/api_keys.py:68-72` — no change to storage; document semantic in docstring: `null = all`, list = prefix allowlist
- Create: `backend/tests/unit/core/test_api_key_endpoint_scope.py`

**Design:**

```python
def _endpoint_allowed(allowed: Optional[list[str]], path: str) -> bool:
    if not allowed:
        return True
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in allowed)
```

Enforce in dependency after hash lookup; on deny → 403 `detail="API key not permitted for this endpoint"`. Store JSON-parse once at validation. Exact-match entries stay exact.

**Tests:** scoped key hits allowed prefix → 200; other path → 403; unscoped key → all allowed.

---

### Task 7: I6 — WS v2 tenant scoping + status gating

Ledger ID: I6. PR: `fix/I6-ws-tenant-scoping`.

**Files:**
- Modify: `backend/src/api/realtime/websocket_v2.py:502-518` (`get_user_connections`), `:624-644` (`test-connection`), `:429-499` (`/status`)
- Test: `backend/tests/unit/api/realtime/` (extend)

**Design:**
1. `get_user_connections` / `test-connection`: after role check, load target user; deny unless `current_user.organization_id == target.organization_id` (403 same detail). Self-access unchanged.
2. `/status`: require `require_admin` dep; scope returned counts to caller org where manager API allows, else admin-global (accept global for admins — decision: gate is the fix; stats remain global for admins).
3. `realtime_service.py:306` client-supplied `user_id` (I15) — fix here too if trivial (use authenticated id), else leave for Task 10 deletion PR.

**Tests:** org-A admin → org-B user connections → 403; same org → 200; non-admin `/status` → 403.

---

### Task 8: I9 — no literal Neo4j password fallbacks in live endpoints

Ledger ID: I9. PR: `fix/I9-neo4j-password-required`.

**Files:**
- Modify: `backend/src/api/arxiv/arxiv_bulk.py:178`, `backend/src/api/arxiv/arxiv_llm_bulk.py:366`, `backend/src/services/ingestion/kaggle_llm_bulk_ingestion.py:112`, `backend/src/services/ingestion/kaggle_bulk_ingestion.py:105`
- Reuse: `src.core.config.settings.NEO4J_PASSWORD` (validator already gates strict envs — Task 1)

**Design:** single helper `get_neo4j_credentials()` (e.g. in `src/core/config.py` or `src/services/ingestion/_neo4j.py`): returns env values; raises `RuntimeError("NEO4J_PASSWORD must be set")` when empty. Replace all four `os.getenv(..., "password")` sites. Local dev unaffected (settings fallback `"neo4jpassword"` for throwaway envs via Task 1 validator — use `settings.NEO4J_PASSWORD`).

**Tests:** unset env + strict env → RuntimeError; settings-based path returns value. grep guard: `grep -rn '"password"' backend/src | grep -i neo4j` → zero.

---

### Task 9: I12 — kill dead RBAC middleware, fix broken encryption-endpoint guards

Ledger ID: I12. PR: `fix/I12-rbac-deadcode-and-encryption-guards`.

**Files:**
- Delete: `backend/src/middleware/rbac.py` (unmounted, broken — verified: no `add_middleware(RBACMiddleware)` in `main.py`)
- Modify: `backend/src/api/security/encryption.py:176-177,229,277,359,401,439,473,542` — replace analytics `@require_permission(["encryption:manage"])` with tenant permission dep from `src/middleware/multi_tenancy.py` (`require_tenant_permission("encryption:manage")`-style dependency)
- Modify: `backend/src/middleware/README.md` (remove RBAC section)
- Check first: `grep -rn "middleware.rbac\|require_role\|RBACMiddleware" backend/src backend/tests` — update any references

**Design:** single shared dep factory in `multi_tenancy.py`:

```python
def require_tenant_permission(permission: str):
    async def _dep(request: Request) -> TenantContext: ...
    return _dep
```

403 (PermissionDeniedException) when missing; 401 when no tenant context. Endpoints become reachable-but-guarded instead of always-500.

**Tests:** authed user without permission → 403; with permission → 200; unauthenticated → 401 (was the only case that worked).

---

## Wave 3 — LOW hygiene batches

### Task 10: I10 + I14 + I15 — dead-code deletion batch

PR: `fix/I10-I15-deadcode-batch`. Ledger IDs: I10, I14, I15 (I15 only if not done in Task 7).

- Delete `backend/src/middleware/file_upload_security.py` (verified zero importers). Fixes I10 by removing illusion; real upload hardening (ClamAV/zip-bomb/svg policy) = separate feature work if wanted — file follow-up finding in ledger as `wontfix (superseded by active path validation)` for the dead service, and open NEW finding for svg policy if user wants it.
- Delete `backend/src/services/config/visualization_config.py`, `knowledge_graph_config.py` (zero importers), and prune JWT/NEO4J fields from `analytics_config.py` (imported only by `analytics_scheduler.py`; grep field usage first — `config.JWT_SECRET_KEY` has zero users).
- Delete `backend/src/services/core/auth.py` (zero importers, `"your-secret-key"` JWT sign/verify).
- `realtime_service.py:306`: use authenticated user id; if its router stays unregistered, delete route file instead (decide by grep at execution time).
- Tests: full unit suite green; grep proof of no remaining imports.

### Task 11: I11 — WS inbound caps

PR: `fix/I11-ws-inbound-caps`.

- `websocket_v2.py` / `websocket_manager.py`: app-level frame check — reject > 64 KiB (close 1009), per-connection token-bucket (e.g. 30 msg/10s → close 1013), bound `client_info`/`message_filter` dict sizes (cap keys, drop-on-update).
- Uvicorn runner: set `ws_max_size` in deployment (check `backend/docker/` + k8s manifests — code-side cap is authoritative).
- Tests: oversized frame → closed 1009; flood → 1013.

### Task 12: I16 + I20 + I21 + I19 — small correctness batch

PR: `fix/I16-I21-small-batch`.

- I16 `src/core/caching.py:44`: derive cache-HMAC via HKDF from `SECRET_KEY` with distinct salt `b"cache-integrity"` — never raw reuse.
- I20 `src/models/entity.py:300`, `src/models/knowledge_graph.py:472`: escape `%`/`_`/`\` in search term (reuse `_escape_like` from `src/api/documents/files.py:51` — move to `src/utils/` shared helper, import both places).
- I21 `src/core/security.py:415-417` + `_extract_*_token_data`: treat missing `exp` as invalid (`if not token_data.exp: raise credentials_exception`) for externally-verifiable tokens; keep CLI hard age cap. Check Supabase tokens always carry exp (they do) — guard is belt-and-braces.
- I19 `src/services/arxiv/arxiv_service.py:669-704`: validate `pdf_url` — scheme https, host allowlist `{arxiv.org, www.arxiv.org, export.arxiv.org}` unless caller is internal; else ignore param and use canonical URL. (Narrowest fix; full SSRF helper = follow-up if other outbound-user-URL sinks appear.)

### Task 13: I13 + I17 + I22 + I23 + I25 + I27 + I26 — config/info-hygiene batch

PR: `fix/hygiene-config-batch`.

- I13 `config.py:162`: `TRUSTED_PROXY_ENABLED: bool = False` + deploy manifests set `true` behind ingress (note in `deployment/` PR description; code default = safe).
- I17 `main.py:662-670`: `/health` returns `{"status","timestamp"}`; move version/env to `/debug/info` (admin-only).
- I22 `cli_auth.py`: accept `poll_token` via `X-CLI-Poll-Token` header (keep query param for one release, log deprecation); never include credential payload in URL.
- I23 `config.py:171-173`: keep `*.gen-text.app` only if subdomain trust is real; else narrow to explicit hosts (check deployment ingress hostnames — decision point, default narrow). `api_gateway.py:127-130`: dead service — delete in Task 10 instead if unregistered (verify); else `allow_origins` never `*` with credentials.
- I25 `core/database.py:356-374`: print seed passwords only when `ENVIRONMENT` in throwaway set AND `SEED_SHOW_PASSWORDS=1`.
- I27: replace hardcoded DSNs in `rebuild_kg_llm.py:452`, `setup_supabase_storage.py:77` with env reads (alembic.ini local value is convention — leave, it's localhost-scoped).
- I26: decision point — either add `super_admin` to `UserRole` + seed, or repoint `organization_create` checks to platform-admin allowlist (`dependencies.py:108-127`). Default: platform-admin allowlist (no new role).

---

## Wave 4 — live-environment verification (authorization-gated)

### Task 14: verify deployed env actually sets the secrets

Ledger: note under I1/I9/I13 rows. **Requires cluster access authorization (nous-cluster-debug skill). NOT RUN by default.**

1. `kubectl -n <dev-ns> get deploy backend -o yaml` → check env keys present (values never printed): `JWT_SECRET_KEY`, `SUPABASE_JWT_SECRET`, `NEO4J_PASSWORD`, `TRUSTED_PROXY_ENABLED`, `ENVIRONMENT`.
2. If `JWT_SECRET_KEY` unset in shared dev → I1 is live-exploitable TODAY: rotate secret immediately (coordination: invalidates CLI tokens — expected).
3. Record outcome in ledger Log.

---

## Execution protocol (every task)

1. `git fetch origin develop && git worktree add` (or feature branch off develop in primary checkout — primary is dirty with 6 unrelated files; **use a worktree**: `../rag-fix-<id>`).
2. Read ledger; claim IDs (Status=claimed, Owner=session) **before** branching.
3. TDD per writing-plans: failing tests → minimal fix → green.
4. Full unit suite: `cd backend && pytest tests/unit -m unit -q` before commit.
5. Commit message: `fix(security): <what> (audit <IDs>)`.
6. Open PR referencing ledger IDs in description; ledger rows → `Status=pr`, `PR=#NNNN`.
7. After merge: rows → `merged`; merge babysitting via `/nous-merge-loop` if running unattended.

**Parallelization:** Tasks 1, 2, 3 fully independent (parallel sessions/worktrees). Tasks 4–9 independent of each other but touch distinct files — safe parallel. Tasks 10–13 sequential-ish (both touch config/dead-code overlaps: run 10 before 13).

**Estimated PR count:** 13.
