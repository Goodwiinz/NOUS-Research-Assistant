# Middleware

Request/response pipeline for the NOUS FastAPI backend. Starlette processes
`add_middleware` registrations in reverse order, so the last one registered
executes first on the inbound path and last on the outbound path.

## Execution order (outermost → innermost, i.e., registration reverse)

1. **SecurityHeadersMiddleware** — last registered, outermost: attaches security
   response headers to every response, including short-circuits from inner layers
   (CORS preflight, rate-limit 429, trusted-host 400).
2. **Request timing / logging** (`@app.middleware("http")`) — adds
   `X-Process-Time` header; logs method, path, client IP, status, and duration.
3. **TrustedHostMiddleware** (production only) — validates `Host` header against
   an explicit allowlist; k8s probe paths (`/health`, `/metrics`, …) are exempt.
4. **MultiTenancyMiddleware** — verifies Bearer JWT, resolves or JIT-provisions
   the user and organisation, sets `tenant_context`/`user_context`/`role_context`
   thread-locals, and tags the Sentry scope. Skips auth and docs paths.
5. **AnalyticsRateLimitMiddleware** — sliding-window rate limiting scoped to
   `/api/*analytics*` paths. Uses Redis sorted-sets when available; falls back to
   an in-process deque. Roles carry different budgets (USER 100/hr, ANALYST
   500/hr, ADMIN 2 000/hr); heavy reports and exports each have a separate bucket
   (10/hr and 20/hr respectively). Returns `Retry-After` on 429.
6. **CORSMiddleware** — explicit allowlist only (`settings.cors_origins_list`);
   no wildcards. Methods, headers, and exposed headers are each individually
   enumerated. Preflight responses are cached for `CORS_MAX_AGE` (24 h by
   default).

`AuditMiddleware`, `EncryptionMiddleware`, and
`APISecurityMiddleware` are implemented in this package but are not currently
wired into `main.py`. (`RBACMiddleware` was removed in audit I12 — it was
never mounted and its decorators were broken; see `rbac.py` below.)

## Key files

| File                       | Class / export                                                            | Purpose                                                                                                                                                                                                                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `security_headers.py`      | `SecurityHeadersMiddleware`                                               | Attaches `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, a strict CSP (no `unsafe-inline`/`unsafe-eval`), and — in production — `Strict-Transport-Security`. Swagger/ReDoc paths are CSP-exempt. Uses `setdefault` so inner layers can still override. |
| `multi_tenancy.py`         | `MultiTenancyMiddleware`                                                  | JWT decode → DB org lookup → JIT user provisioning. Sets `tenant_context`, `user_context`, `role_context` ContextVars used by RBAC helpers. Includes PostgreSQL RLS helpers and `TenantAwareQuery` for service-layer tenant isolation.                                                                      |
| `rate_limiting.py`         | `AnalyticsRateLimitMiddleware`, `RedisRateLimiter`, `InMemoryRateLimiter` | Sliding-window rate limiting for analytics endpoints. Redis-backed when available; in-memory deque as fallback. Emits standard `X-RateLimit-*` headers.                                                                                                                                                     |
| `api_security.py`          | `APISecurityMiddleware`                                                   | Regex-based pattern matching for SQL injection, XSS, path traversal, and command injection in URLs, headers, and JSON/form bodies. Also does IP blocking (in-memory), per-IP request anomaly detection, and logs security events. Body size limit 50 MB. _Not currently registered._                        |
| `rbac.py`                  | `require_permission_dep`                                                  | FastAPI dependency enforcing a named permission via `RBACService` with the request-scoped sync session (401 unauthenticated, 403 missing permission). Used by the compliance, RBAC-management, and encryption routers. Audit I12 removed the never-mounted `RBACMiddleware` class and the broken `@require_*` decorators (they called `RBACService()` with no session → always 403). |
| `audit.py`                 | `AuditMiddleware`                                                         | Full request/response audit trail via `AuditService`. Masks `Authorization`, `Cookie`, and `X-API-Key` headers; redacts auth path bodies. _Not currently registered._                                                                                                                                       |
| `encryption_middleware.py` | `EncryptionMiddleware`                                                    | Field-level encryption of sensitive request fields (SSN, API keys, passwords, etc.) and PII masking in responses. _Not currently registered._                                                                                                                                                               |
| `query_monitor.py`         | `QueryMonitorMiddleware`                                                  | SQLAlchemy event listener + ContextVar to collect per-request query stats: count, total time, slow-query threshold (>100 ms), N+1 detection heuristics.                                                                                                                                                     |
| `responses.py`             | `error_response`                                                          | Shared helper used by several middlewares to emit a uniform JSON error envelope.                                                                                                                                                                                                                            |

## Notable design decisions

- **CORS allowlist**: origins, methods, headers, and exposed headers are all
  explicit; no wildcards. See `settings.cors_*_list` in `src/core/config.py`.
- **WebSocket auth**: uses `Sec-WebSocket-Protocol` header rather than URL query
  params to avoid token leakage in access logs and proxy caches.
- **SQL injection prevention**: `api_security.py` scans all input surfaces at
  the HTTP layer; service-layer queries use validated enums from
  `src/shared/enums.py` so raw sort/filter strings are never interpolated.
- **`APISecurityMiddleware` vs `SecurityHeadersMiddleware`**: the former was
  implemented but never registered because its `dispatch` bundles request
  blocking with header attachment; `security_headers.py` was extracted to add
  the headers safely without the behavioural side effects.
