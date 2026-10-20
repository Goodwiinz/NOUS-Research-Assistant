# core

Shared infrastructure consumed by every other package in the backend. Nothing
in `src/services/`, `src/api/`, or `src/models/` should import from a peer
layer directly — it should import from `core` instead.

## Key files

| File                   | Purpose                                                                                                                                                                                                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config.py`            | `pydantic-settings` `Settings` class; single source of truth for all env vars. Validates CORS regex anchoring, SECRET_KEY/JWT strength, DATABASE_URL scheme, and DO Knowledge Base field cohesion. Auto-generates dev secrets; hard-fails in production/staging on weak values. |
| `database.py`          | SQLAlchemy async engine, `AsyncSessionLocal`, and `get_db` dependency. Resolves `DATABASE_URL` vs `SUPABASE_DB_URL`, wires pool size from env, attaches query-timing listeners.                                                                                                 |
| `security.py`          | JWT verification for both Supabase-issued tokens (HS256 shared secret or ES256 via JWKS) and long-lived CLI device tokens (`scope=cli`). Provides `get_current_user_token`, password hashing, and a `RateLimiter` class backed by Redis.                                        |
| `dependencies.py`      | FastAPI `Depends` helpers: `get_current_user` (loads `User` + org via eager join), role-enforcement guards, and optional-auth variants.                                                                                                                                         |
| `supabase_client.py`   | Lazy singleton for the Supabase service-role client; also provides Supabase Storage helpers. Returns `None` gracefully when credentials are absent.                                                                                                                             |
| `openai_endpoint.py`   | `classify_openai_endpoint()` — distinguishes Azure resource endpoints from OpenAI-compatible (`/v1`) endpoints so the agent graph can pick the right LangChain client.                                                                                                          |
| `circuit_breaker.py`   | Circuit-breaker pattern (closed/open/half-open) for external dependencies such as Neo4j and Cohere (the module docstring still mentions Qdrant, which is removed). Raises `ServiceUnavailableError` when open.                                                                                                                                                      |
| `resilience.py`        | Retry with exponential backoff (`RetryStrategy.EXPONENTIAL`), bulkhead concurrency limits, and timeout wrappers. Composes with `circuit_breaker.py` for full resilience.                                                                                                        |
| `caching.py`           | Redis-backed decorator caching with TTL, HMAC-signed cache keys, gzip compression, Prometheus hit/miss counters, and probabilistic early expiry to prevent stampedes.                                                                                                           |
| `rate_limit.py`        | Abstract `RateLimiterInterface` + Redis sliding-window implementation. Separate `is_allowed` / `record_attempt` methods let read and write paths decouple.                                                                                                                      |
| `encryption.py`        | AES-256-GCM encryption for data at rest, field-level column encryption, file encryption, and PBKDF2/HKDF key derivation. Key rotation support.                                                                                                                                  |
| `websocket_auth.py`    | Authenticates WebSocket upgrades via the `Sec-WebSocket-Protocol` header (not URL query params) to prevent token exposure in logs and browser history.                                                                                                                          |
| `api_key_auth.py`      | Long-lived API key scheme for programmatic access: SHA-256 hashed storage, Redis-backed rate limiting, `HTTPBearer` integration.                                                                                                                                                |
| `s3_client.py`         | Lazy boto3 singleton for DigitalOcean Spaces (S3-compatible). Returns `None` when `S3_ENDPOINT_URL`/keys are absent; used by the `supabase` storage backend fallback.                                                                                                           |
| `metrics.py`           | Thread-safe in-process counters (`record_request_count`, `record_duration`) consumed by legacy health endpoints. Thin shim; Prometheus is the canonical metrics path.                                                                                                           |
| `pool_monitoring.py`   | SQLAlchemy pool event listeners that emit checkout/checkin/overflow/timeout metrics and structured log events for capacity alerting.                                                                                                                                            |
| `cache_warmup.py`      | `warm_critical_caches()` — non-blocking startup task that pre-populates Redis from persistent storage; individual sub-task failures are caught and logged.                                                                                                                      |
| `init_db.py`           | One-shot schema creation and seed-data helper for local/CI bringup (`create_all`, default org/user bootstrap). Not called at runtime.                                                                                                                                           |
| `user_provisioning.py` | `ensure_user_and_org()` — idempotent upsert that creates a `User` row and free-tier `Organization` on first Supabase login.                                                                                                                                                     |
| `async_utils.py`       | `reraise_if_cancelled()` — re-raises `asyncio.CancelledError` that surfaced as a return value from `gather(return_exceptions=True)`.                                                                                                                                            |
| `neo4j_auth.py`        | Neo4j credential resolution for ingestion paths: `NEO4J_PASSWORD` is required at call time, there is no literal fallback password.                                                                                                                                              |
| `probes.py`            | `PROBE_EXEMPT_PATHS`: the kubelet probe paths exempted from `TrustedHostMiddleware` (probes send `Host: <pod-IP>`). Dependency-light so it is unit-testable.                                                                                                                       |
| `cli_token_revocation.py` | Revokes CLI tokens via a per-user Redis "revoked before" timestamp checked by `get_current_user`; failure semantics on a store outage are configurable.                                                                                                                         |

## Subdirectories

| Directory                 | Contents                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ai/`                     | Protocol classes for AI client duck-typing (`protocols.py`), Pydantic schemas for LLM judge responses (`schemas.py`), and JSON/markdown response parsers with `AIResponseParseError` fallback (`parsers.py`).                                                                                                                                                                              |
| `prompts/`                | `tone_prompts.py` — system prompt strings for the Scholarly Tone Engine (academic, simplified, concise, expanded), each with a citation-preservation instruction prepended.                                                                                                                                                                                                                |

## Configuration conventions

All settings are declared as `pydantic-settings` fields on `Settings` in
`config.py`. Env vars map 1-to-1 by field name (case-sensitive). Comma-delimited
strings (`CORS_ORIGINS`, `CORS_ALLOWED_HEADERS`) have matching `@property`
helpers that return `List[str]`. Validators run at import time; a bad value
causes a `ValidationError` before the FastAPI app binds a port.

Development defaults are safe to use locally; several secrets auto-generate
via `secrets.token_urlsafe(32)` rather than shipping hardcoded strings. In
`production` and `staging`, any weak or absent secret is a startup error.

The `settings` singleton is module-level. Import it as:

```python
from src.core.config import settings
```

For FastAPI dependency injection use `get_settings()` or inject
`Depends(get_settings)` where you need testable overrides.
