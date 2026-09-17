"""
Application configuration settings

SECURITY NOTE: All sensitive configuration values MUST be provided via environment
variables. Default values are only used for local development.
"""

import re
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings

_LOCAL_SECRET_KEY = "local-development-secret-key-not-for-production"
_LOCAL_JWT_SECRET_KEY = "local-development-jwt-secret-not-for-production"


# ``ENVIRONMENT`` values that mark an explicit local/CI throwaway process.
# Only these environments may silently degrade LangGraph durability
# (checkpointer / long-term memory store) to in-memory backends when the
# Postgres init fails. Deliberately does NOT include ``dev``: the DOKS
# ``dev`` deployment is a shared, long-lived environment (Supabase
# Postgres, real users + synthetic traffic) where a silent MemorySaver /
# InMemoryStore fallback breaks HITL resume and cross-restart memory
# invisibly (system-design audit finding X3).
MEMORY_FALLBACK_ENVIRONMENTS = frozenset(
    {"development", "testing", "local", "test", "ci"}
)


def _longest_literal_hostname_run(pattern: str) -> int:
    """Longest contiguous literal hostname segment (project slug specificity)."""
    pattern = re.sub(r"\[[^\]]+\](?:[+*?]|\{[^}]+\})?", "", pattern)
    pattern = re.sub(r"\([^)]*\)(?:[+*?]|\{[^}]+\})?", "", pattern)
    runs = re.findall(r"[A-Za-z0-9-]+", pattern)
    return max((len(run) for run in runs), default=0)


def _first_hostname_label(pattern: str) -> str:
    """Return the first hostname label from an anchored origin regex."""
    origin_pattern = pattern.removeprefix("^").removesuffix("$")
    for scheme in ("https://", "http://", "https?://"):
        if origin_pattern.startswith(scheme):
            origin_pattern = origin_pattern[len(scheme) :]
            break
    hostname_pattern = origin_pattern.split("/", 1)[0].split(":", 1)[0]
    return re.split(r"\\\.|\.", hostname_pattern, maxsplit=1)[0]


def _cors_origin_regex_is_overbroad(pattern: str) -> bool:
    """Detect regex shapes unsafe with allow_credentials=True."""
    if pattern in {
        "^https://.*$",
        "^https://.+$",
        "^https?://.*$",
        "^https?://.+$",
    }:
        return True
    if ".*" in pattern or ".+" in pattern:
        return True
    if "(.*)" in pattern or "(.+)" in pattern:
        return True
    return False


class Settings(BaseSettings):
    """Application settings"""

    # Application
    APP_NAME: str = "Multimodal Enterprise RAG System"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    SECRET_KEY: str = ""

    # Break-glass override for durable agent state: when True, the LangGraph
    # checkpointer / memory store may fall back to non-durable in-memory
    # backends on Postgres init failure even in a shared environment.
    # The fallback silently breaks HITL resume and cross-restart memory
    # while chat_messages keep persisting — set this only to keep a
    # deployment limping through a known Postgres outage (values flip, no
    # image rebuild), and revert as soon as the outage is over.
    ALLOW_MEMORY_FALLBACK: bool = False

    @property
    def is_throwaway_environment(self) -> bool:
        """Whether ``ENVIRONMENT`` names an explicit local/CI throwaway process.

        Keyed on ``MEMORY_FALLBACK_ENVIRONMENTS`` — the single source of truth
        for "is this a disposable local/test/CI boot?" (``development``,
        ``testing``, ``local``, ``test``, ``ci``). Deliberately excludes
        ``dev``: the DOKS ``dev`` deployment is a shared, long-lived,
        load-balanced environment and must behave like a strict env for
        readiness / durability gating.

        Reuse this predicate anywhere behaviour must relax only for throwaway
        boots (agent-state durability, readiness gating) instead of
        re-deriving an env allowlist — a drifted second/third list is how the
        live ``ENVIRONMENT=dev`` deployment slips through the wrong branch.
        """
        return self.ENVIRONMENT.strip().lower() in MEMORY_FALLBACK_ENVIRONMENTS

    @property
    def require_durable_agent_state(self) -> bool:
        """Whether agent state (checkpointer / memory store) must be durable.

        True by default: any shared or deployed environment (``dev``,
        ``staging``, ``production``, or anything unrecognised) must fail
        loudly when the Postgres-backed checkpointer or memory store cannot
        initialise, instead of silently degrading to in-memory state.

        False only when the environment is an explicit local/CI throwaway
        (``is_throwaway_environment``) or the ``ALLOW_MEMORY_FALLBACK``
        break-glass override is set. Keyed on "is this a throwaway
        process?", never on a hard-coded allowlist of strict env names —
        the old ``("production", "staging")`` gate never fired in the live
        ``ENVIRONMENT=dev`` deployment (audit finding X3).
        """
        if self.ALLOW_MEMORY_FALLBACK:
            return False
        return not self.is_throwaway_environment

    # Canonical public URL of the frontend (e.g. https://www.goodwiinz.tech).
    # Used to build redirect targets like the CLI device-flow auth page.
    # Decoupled from CORS_ORIGINS so reordering the allowlist can't break login.
    # Empty falls back to cors_origins_list[0] for backward compatibility.
    FRONTEND_BASE_URL: str = ""

    # CORS Configuration (comma-separated string from env, parsed to list)
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    # Optional regex pattern (e.g. ^https://nous-platform-[a-z0-9-]+\.vercel\.app$).
    # Sourced from Infisical /app per env so the project slug stays out of the
    # chart. Empty disables.
    CORS_ORIGIN_REGEX: str = ""
    CORS_ALLOWED_HEADERS: str = (
        "Authorization,"
        "Content-Type,"
        "Accept,"
        "Origin,"
        "X-Request-ID,"
        "X-Correlation-ID,"
        "X-Client-Version,"
        "Cache-Control,"
        # SSE resume cursor (agentChatService.resumeStream). Not a CORS-safelisted
        # header, so cross-origin preflight (Vercel frontend -> dev-api backend)
        # rejects the request unless it is allowlisted here.
        "Last-Event-ID"
    )
    CORS_ALLOWED_METHODS: str = "GET,POST,PUT,DELETE,PATCH,OPTIONS,HEAD"
    CORS_EXPOSE_HEADERS: str = "X-Request-ID,X-Correlation-ID,X-Process-Time"
    CORS_MAX_AGE: int = 86400  # 24 hours preflight cache

    # R4-M13: gate the process-wide X-Forwarded-For patch (security.py
    # _install_proxy_aware_client_patch) that makes request.client.host /
    # get_client_ip() trust the (rightmost) XFF entry. Default True to
    # preserve current deployed-dev behavior — we sit behind our own ingress,
    # which appends the real client IP as the last hop. Set False for any
    # deployment NOT behind a trusted reverse proxy, where trusting a
    # client-suppliable header would let a client spoof its own IP for
    # rate-limiting / audit-log / abuse-detection purposes.
    TRUSTED_PROXY_ENABLED: bool = True

    # Host header allow-list for TrustedHostMiddleware (production only; see
    # main.py). Comma-separated so an environment can add its own hostname
    # without editing app code: a server-side Next.js rewrite proxies with the
    # DESTINATION host in the Host header, so any deployment that reaches the
    # backend through a rewrite must allow-list that hostname (CI compose does
    # this for the `backend` service name). Starlette strips the port before
    # matching, so entries never carry one.
    TRUSTED_HOSTS: str = (
        "localhost,127.0.0.1,testserver,*.gen-text.app,*.svc.cluster.local"
    )

    @property
    def trusted_hosts_list(self) -> List[str]:
        """Parse TRUSTED_HOSTS string into a list."""
        return [h.strip() for h in self.TRUSTED_HOSTS.split(",") if h.strip()]

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS_ORIGINS string into a list."""
        if not self.CORS_ORIGINS:
            return ["http://localhost:3000"]
        return [
            origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()
        ]

    @property
    def cors_headers_list(self) -> List[str]:
        """Parse CORS_ALLOWED_HEADERS string into a list."""
        return [h.strip() for h in self.CORS_ALLOWED_HEADERS.split(",") if h.strip()]

    @property
    def cors_methods_list(self) -> List[str]:
        """Parse CORS_ALLOWED_METHODS string into a list."""
        return [m.strip() for m in self.CORS_ALLOWED_METHODS.split(",") if m.strip()]

    @property
    def cors_expose_list(self) -> List[str]:
        """Parse CORS_EXPOSE_HEADERS string into a list."""
        return [h.strip() for h in self.CORS_EXPOSE_HEADERS.split(",") if h.strip()]

    @field_validator("CORS_ORIGIN_REGEX")
    @classmethod
    def validate_cors_origin_regex(cls, v: str, info: ValidationInfo) -> str:
        """Reject overly broad origin regexes used with credentialed CORS."""
        if not v or not v.strip():
            return ""

        pattern = v.strip()
        if not pattern.startswith("^") or not pattern.endswith("$"):
            raise ValueError("CORS_ORIGIN_REGEX must be anchored with ^ and $")

        env = str(info.data.get("ENVIRONMENT", "development"))
        if env in ("production", "staging"):
            if not pattern.startswith("^https://"):
                raise ValueError(
                    "CORS_ORIGIN_REGEX must use ^https:// in production/staging"
                )
        elif not (pattern.startswith("^https://") or pattern.startswith("^http://")):
            raise ValueError("CORS_ORIGIN_REGEX must start with ^https:// or ^http://")

        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"CORS_ORIGIN_REGEX is invalid: {exc}") from exc

        if _cors_origin_regex_is_overbroad(pattern):
            raise ValueError(
                "CORS_ORIGIN_REGEX is too permissive for credentialed CORS"
            )

        if _longest_literal_hostname_run(_first_hostname_label(pattern)) < 6:
            raise ValueError(
                "CORS_ORIGIN_REGEX must include a project-specific literal "
                "hostname segment (at least 6 characters, e.g. nous-platform)"
            )

        return pattern

    # Database
    DATABASE_URL: str = (
        "postgresql://postgres:postgres@localhost:5432/multimodal_rag_dev"
    )

    # Supabase
    SUPABASE_URL: str = "http://localhost:54321"
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_DB_URL: str = ""  # If set, overrides DATABASE_URL for Supabase connection
    SUPABASE_JWT_SECRET: str = ""  # Supabase JWT secret for verifying auth tokens
    # Optional defense-in-depth: when set, verify_token pins the Supabase JWT
    # `iss` claim to this EXACT value. Leave empty to skip issuer validation —
    # the token signature already binds to SUPABASE_JWT_SECRET/JWKS. Not derived
    # from SUPABASE_URL: the hosted stack issues `<url>/auth/v1` while a bare
    # GoTrue (CI/local) issues a different value, so the exact string must be
    # stated explicitly (e.g. https://<ref>.supabase.co/auth/v1) to enable it.
    SUPABASE_JWT_ISSUER: str = ""

    REDIS_URL: str = "redis://localhost:6379"

    # Neo4j Configuration
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""

    # Figure extraction (PyMuPDF, Phase 1) — embedded raster figures + caption
    # heuristics during PDF ingestion. Off by default; flip per-env in Infisical.
    FIGURE_EXTRACTION_ENABLED: bool = False

    # DigitalOcean Knowledge Base (GenAI Platform / GradientAI)
    # Public Preview — API may churn. One KB per organization.
    DO_KB_ENABLED: bool = False
    DO_KB_PRIMARY_READ: bool = False  # Phase 4b: DO KB serves reads
    DO_API_TOKEN: Optional[str] = None
    DO_KB_REGION: Optional[str] = None  # e.g. "tor1", "nyc3"
    DO_KB_PROJECT_ID: Optional[str] = None
    DO_KB_EMBEDDING_MODEL_UUID: Optional[str] = None
    DO_KB_API_HOST: str = "https://api.digitalocean.com"
    DO_KB_RETRIEVE_HOST: str = "https://kbaas.do-ai.run"
    DO_KB_DEFAULT_TOP_K: int = 8
    DO_KB_RETRIEVE_ALPHA: Optional[float] = 0.5
    DO_KB_REQUEST_TIMEOUT_SECONDS: float = 30.0
    # Hot-path retrieval cap: on timeout the agent falls back to hybrid search.
    # Live traces: p50=0.6s, p95=1.5s — 3 s leaves headroom without masking
    # real failures.  DO_KB_REQUEST_TIMEOUT_SECONDS (30 s) still governs all
    # other DO KB HTTP calls (index, list, delete …).
    DO_KB_RETRIEVE_TIMEOUT_SECONDS: float = 3.0
    DO_KB_INDEXING_TIMEOUT_SECONDS: float = 120.0
    # Pre-flight guard: PDFs over EITHER threshold get text-extracted locally
    # before DO KB sync, so the canonical .txt path is used instead of the raw
    # PDF (DO's server-side parser times out on large/complex PDFs).
    DO_KB_FORCE_TEXT_PDF_PAGES: int = 100
    DO_KB_FORCE_TEXT_PDF_SIZE_MB: int = 5

    # JWT Configuration
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # Default refresh token lifetime
    REMEMBER_ME_REFRESH_TOKEN_DAYS: int = 30  # Extended session for "Remember Me"
    CLI_TOKEN_EXPIRE_DAYS: int = 30  # Long-lived CLI device tokens
    # When True, a CLI-token revocation check that cannot reach the revocation
    # store (Redis error / outage) DENIES the token (fail-closed) instead of
    # the historical fail-open. This hardens against a Redis outage or failover
    # silently un-revoking every revoked CLI token, at the cost of CLI auth
    # availability while the store is unreachable. A plain miss (store reachable,
    # no revoked-before cutoff for the user) still allows regardless of this
    # flag. Default False preserves current behavior — flip to True to harden;
    # rollback is a flag flip, no logic redeploy. (audit D7)
    CLI_TOKEN_REVOCATION_FAIL_CLOSED: bool = False

    @model_validator(mode="after")
    def _enforce_debug_off_in_prod(self):
        """Never allow DEBUG=True in production or staging."""
        if self.ENVIRONMENT in ("production", "staging"):
            self.DEBUG = False
        return self

    @model_validator(mode="after")
    def _override_database_url_from_supabase(self):
        """Override DATABASE_URL when SUPABASE_DB_URL is set."""
        if self.SUPABASE_DB_URL:
            self.DATABASE_URL = self.SUPABASE_DB_URL
        # Validate final DATABASE_URL (allow sqlite in testing)
        allowed_prefixes = ("postgresql://", "postgresql+asyncpg://")
        if self.ENVIRONMENT == "testing":
            allowed_prefixes = ("postgresql://", "postgresql+asyncpg://", "sqlite://")
        if not self.DATABASE_URL.startswith(allowed_prefixes):
            raise ValueError(
                "DATABASE_URL must start with postgresql:// or postgresql+asyncpg://"
            )
        if (
            self.ENVIRONMENT in ("production", "staging")
            and "localhost" in self.DATABASE_URL
        ):
            raise ValueError(
                "DATABASE_URL must not point to localhost in production/staging"
            )
        return self

    @field_validator("SECRET_KEY", mode="before")
    @classmethod
    def validate_secret_key(cls, v, info):
        """Validate SECRET_KEY - require in production, generate for dev."""
        weak_patterns = [
            "change-in-production",
            "your-secret",
            "changeme",
            "secret-key",
            "dev-secret",
        ]
        is_weak = not v or any(
            pattern in (v or "").lower() for pattern in weak_patterns
        )

        if is_weak:
            env = str(info.data.get("ENVIRONMENT", "development"))
            if env in ("production", "staging"):
                raise ValueError(
                    "SECRET_KEY must be set to a strong value in production/staging"
                )
            return _LOCAL_SECRET_KEY
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("JWT_SECRET_KEY", mode="before")
    @classmethod
    def validate_jwt_secret_key(cls, v, info):
        """Validate JWT_SECRET_KEY - require in production, generate for dev."""
        weak_patterns = [
            "change-in-production",
            "your-secret",
            "changeme",
            "jwt-secret",
            "dev-jwt-persistent",
        ]
        is_weak = not v or any(
            pattern in (v or "").lower() for pattern in weak_patterns
        )

        if is_weak:
            env = str(info.data.get("ENVIRONMENT", "development"))
            if env in ("production", "staging"):
                raise ValueError(
                    "JWT_SECRET_KEY must be set to a strong value in production/staging. "
                    "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
                )
            return _LOCAL_JWT_SECRET_KEY
        if len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("NEO4J_PASSWORD", mode="before")
    @classmethod
    def validate_neo4j_password(cls, v, info: ValidationInfo):
        """Validate NEO4J_PASSWORD - require in production."""
        if not v or v == "neo4jpassword":
            env = str(info.data.get("ENVIRONMENT", "development"))
            if env in ("production", "staging"):
                raise ValueError(
                    "NEO4J_PASSWORD must be set via environment variable in production/staging"
                )
            return "neo4jpassword"  # Default for local development
        return v

    # File Storage
    UPLOAD_DIR: str = "./uploads"
    MAX_FILE_SIZE_MB: int = 10
    FREE_TIER_STORAGE_GB: int = 10

    # Storage Backend: "local", "s3", or "supabase"
    STORAGE_BACKEND: str = "local"

    # S3-Compatible Object Storage (DigitalOcean Spaces)
    S3_ENDPOINT_URL: Optional[str] = None  # https://nyc3.digitaloceanspaces.com
    S3_ACCESS_KEY: Optional[str] = None
    S3_SECRET_KEY: Optional[str] = None
    S3_BUCKET_NAME: str = "rag-system-storage"
    S3_REGION: str = "nyc3"
    S3_CDN_ENDPOINT: Optional[str] = (
        None  # https://rag-system-storage.nyc3.cdn.digitaloceanspaces.com
    )
    S3_STORAGE_TEMP_DIR: str = "/tmp/rag_s3_storage"

    # Supabase Storage (legacy)
    SUPABASE_STORAGE_ENABLED: bool = False
    SUPABASE_STORAGE_TEMP_DIR: str = "/tmp/rag_storage"

    # Security directories
    SECURITY_DIR: str = "./security"  # Directory for encryption keys and security files

    # Security
    BCRYPT_ROUNDS: int = 12
    RATE_LIMIT_PER_MINUTE: int = 60
    AUTH_RATE_LIMIT_ATTEMPTS: int = 50  # Max auth attempts in window
    AUTH_RATE_LIMIT_WINDOW_MINUTES: int = 15  # Time window for rate limiting

    # UUIDs of explicitly trusted platform operators.  This is intentionally
    # separate from tenant roles: an organization ADMIN must not gain access
    # to process-wide worker controls or global ingestion sinks.  The value is
    # a comma-separated environment setting; parsing is fail-closed so a
    # malformed deployment value authorizes nobody.
    PLATFORM_OPERATOR_USER_IDS: str = ""

    @property
    def platform_operator_user_ids(self) -> frozenset[UUID]:
        """Return the configured platform-operator UUIDs, or none on error."""
        raw = self.PLATFORM_OPERATOR_USER_IDS.strip()
        if not raw:
            return frozenset()

        values = [value.strip() for value in raw.split(",")]
        if not values or any(not value for value in values):
            return frozenset()

        parsed: set[UUID] = set()
        for value in values:
            try:
                parsed.add(UUID(value))
            except (AttributeError, ValueError):
                return frozenset()
        return frozenset(parsed)

    # External APIs
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None

    # Research Connector APIs
    CROSSREF_MAILTO: Optional[str] = None
    NCBI_API_KEY: Optional[str] = None
    OPENALEX_API_KEY: Optional[str] = None
    SEMANTIC_SCHOLAR_API_KEY: Optional[str] = None

    # Azure OpenAI Configuration
    AZURE_OPENAI_API_KEY: Optional[str] = None
    AZURE_OPENAI_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_API_VERSION: str = "2024-02-15-preview"
    AZURE_OPENAI_DEPLOYMENT_NAME: Optional[str] = None
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME: Optional[str] = None
    AZURE_OPENAI_CHAT_DEPLOYMENT_NAME: Optional[str] = None

    # Multiple Endpoints Support
    AZURE_OPENAI_CHAT_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_EMBEDDING_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_CHAT_API_VERSION: str = "2024-06-01"
    AZURE_OPENAI_EMBEDDING_API_VERSION: str = "2023-05-15"

    # Separate API Keys Support
    AZURE_OPENAI_CHAT_API_KEY: Optional[str] = None
    AZURE_OPENAI_EMBEDDING_API_KEY: Optional[str] = None

    # Lightweight model for auxiliary agent tasks (classifier, compactor, etc.)
    #
    # 2026-08-20: nano/mini tiering retired — every role defaults to
    # gpt-5.6-luna. The tool-loop audit (trace 01a0209b) showed the cheap
    # tiers doing the error-recovery reasoning where they measurably
    # misbehaved (blind retry loops, premature "which paper?" stalls).
    # NOTE: env values override these defaults — remove any
    # AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT / AZURE_OPENAI_SYNTHESIS_DEPLOYMENT
    # secrets still pointing at gpt-5-nano / gpt-5-mini (Infisical /do-kb).
    AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT: Optional[str] = "gpt-5.6-luna"

    # Separate deployment for post-tool prose synthesis. Falls back to
    # AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT when unset. Same deployment as the
    # lightweight tier since the nano/mini retirement.
    AZURE_OPENAI_SYNTHESIS_DEPLOYMENT: Optional[str] = "gpt-5.6-luna"

    # Dedicated evidence-independent chat lane. Kept separate from the main
    # deployment so rollout/rollback never changes tool-calling behavior.
    AGENT_FAST_PATH_ENABLED: bool = False
    AGENT_FAST_PATH_DEPLOYMENT: str = "gpt-5.6-luna"
    AGENT_FAST_PATH_MAX_INPUT_CHARS: int = 8_000
    AGENT_FAST_PATH_MAX_OUTPUT_TOKENS: int = 768
    AGENT_FAST_PATH_REQUEST_TIMEOUT: float = 20.0

    # gpt-5 reasoning_effort knobs. Lower = faster.
    # Accepted values: "none" | "minimal" | "low" | "medium" | "high"
    # Defaults tuned for fast responses; raise to "medium" for tougher tasks.
    #
    # Forwarded ONLY when AGENT_USE_RESPONSES_API is on. On Chat Completions
    # Azure rejects reasoning_effort alongside function tools, and every
    # _build_llm consumer binds tools, so the kwarg is dropped there (#1334).
    # See graph.py _build_llm.
    AGENT_MAIN_REASONING_EFFORT: str = "low"

    # Route the main tool-calling deployment through the Azure Responses API
    # (/v1/responses) instead of Chat Completions.
    #
    # This is the only way to keep reasoning_effort on tool-calling turns —
    # the capability #1334 had to give up. Measured 2026-08-03 against
    # gpt-5.6-luna: tools + reasoning_effort up to "max" are accepted over a
    # full Human/AI-with-tool_calls/Tool history.
    #
    # OFF by default because it has a hard prerequisite: the deployment's
    # AZURE_OPENAI_CHAT_API_VERSION must be >= 2025-04-01-preview. Older
    # versions 400 with "Azure OpenAI Responses API is enabled only for
    # api-version...". rag-dev is pinned at 2024-12-01-preview as of this
    # writing, so enabling this without bumping the secret breaks every turn.
    #
    # Responses returns content as typed blocks rather than a string; that is
    # normalised at the node boundary by _nodes_llm.normalize_ai_content, so
    # nothing downstream of the LLM nodes needs to know which API was used.
    AGENT_USE_RESPONSES_API: bool = False
    # Governs classify / plan / reflect / compact / synthesis. Callers that
    # send function tools pass tool_calling=True and drop it entirely — see
    # llm_factory._reasoning_effort_for.
    #
    # "minimal" is NOT universally supported, and the rule is by model
    # generation, not by deployment. Per Azure's reasoning-models doc:
    #
    #   "minimal is only supported with the original GPT-5 reasoning models.
    #    minimal isn't supported with gpt-5.1 or greater."
    #   https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning
    #
    # Confirmed against the rag-dev deployments (2026-08-03):
    #
    #   gpt-5-nano / gpt-5-mini   none | minimal | low | medium | high
    #   gpt-5.6-luna              none |           low | medium | high | xhigh
    #       -> 400 "Unsupported value: 'reasoning_effort' does not support
    #          'minimal' with this model."
    #
    # "none" is the default because it is the only value measured working on
    # all three deployments. "minimal" was the previous default and is a
    # migration hazard twice over: it breaks on any gpt-5.1+ deployment, and
    # _resolve_lightweight_deployment() falls back to the MAIN chat deployment
    # when AZURE_OPENAI_LIGHTWEIGHT_DEPLOYMENT is unset — so a gpt-5.6-only
    # setup 400s on every classifier turn with nothing in the config naming
    # the offending model.
    #
    # Reproduced end to end 2026-08-07 (agent-project-management-v1 Harbor run
    # with every role pointed at gpt-5.6-luna): BadRequestError out of
    # force_synthesis_node, whole turn lost.
    #
    # Related: Azure documents that parallel tool calls are unsupported when
    # reasoning_effort is "minimal", which AGENT_PARALLEL_TOOL_CALLS assumes;
    # "none" sidesteps that coupling too.
    AGENT_LIGHTWEIGHT_REASONING_EFFORT: str = "none"

    # Opt-in semantic router; Azure still owns generation and routing fallback.
    AGENT_INTENT_PROVIDER: Literal["azure", "typesafe"] = "azure"
    TYPESAFE_API_KEY: Optional[SecretStr] = None
    TYPESAFE_MODEL: str = "jev-1.13.0"
    TYPESAFE_TIMEOUT_SECONDS: float = Field(
        default=1.0, gt=0, le=25, allow_inf_nan=False
    )
    # Unset disables acceptance until a deployment-specific calibration is approved.
    TYPESAFE_MIN_CONFIDENCE: Optional[float] = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )

    # Synthesis-only override. Unset (None) inherits
    # AGENT_LIGHTWEIGHT_REASONING_EFFORT, so leaving it alone is a no-op —
    # existing single-knob deployments keep their current behaviour.
    #
    # Set it when prose synthesis needs to think harder than a classifier
    # does. The same per-generation value rules apply as above; on rag-dev's
    # gpt-5.6-luna that means "none" | "low" | "medium" | "high" | "xhigh"
    # and NOT "minimal".
    #
    # Only the two prose-only call sites can actually use this — both named
    # force_synthesis_node, in _nodes_llm and in subgraphs/_factory. The
    # four post-tool callers pass tool_calling=True, and
    # _reasoning_effort_for drops the kwarg for them on Chat Completions —
    # they need AGENT_USE_RESPONSES_API before any value here reaches Azure.
    AGENT_SYNTHESIS_REASONING_EFFORT: Optional[str] = None

    # Bound Azure LLM call wall-clock to prevent model-router hangs. LangSmith
    # has observed traces with end_time=null blocking root for 70s+. Default
    # 60s for main agent LLM (synthesis can be long), 30s for lightweight
    # auxiliary calls (classifier/planner/reflection — should be fast).
    AGENT_LLM_REQUEST_TIMEOUT: float = 60.0
    AGENT_LIGHTWEIGHT_REQUEST_TIMEOUT: float = 30.0
    AGENT_LLM_MAX_RETRIES: int = 2

    # When True, post-tool synthesis turns (final-answer LLM call right after
    # a ToolMessage) use the lightweight deployment instead of the main one.
    # Cuts ~5-15s/turn on read-heavy queries like arxiv search results.
    AGENT_LIGHTWEIGHT_SYNTHESIS: bool = True

    # Run the insight-extraction pass every N user turns inside memory_save_node.
    # 0 disables. Default 5: cheap enough to not bloat token spend, frequent
    # enough to keep recall surface useful within a session.
    AGENT_INSIGHT_EVERY_N_TURNS: int = 5

    # When False, the agent LLM emits at most one tool_call per turn. gpt-5
    # fires runaway parallel batches by default (trace 019e18f0: 5-6 parallel
    # search_arxiv per round, 13+ total over 4 rounds, 95s wall). Flip to
    # True only when comparing two documents in parallel is the explicit
    # user intent.
    #
    # Trap if you do flip it: parallel tool calls are unsupported at
    # reasoning_effort="minimal". Moot today because this is False, but any
    # node still running at "minimal" would break the moment both are on.
    AGENT_PARALLEL_TOOL_CALLS: bool = False

    # Project-skill runtime rollout gates.  All default off so deploying the
    # catalog schema cannot change existing agent behavior until operators
    # explicitly enable the staged path.
    AGENT_TOOL_REGISTRY_ENFORCEMENT_ENABLED: bool = False
    PROJECT_SKILL_CATALOG_ENABLED: bool = False
    PROJECT_SKILL_RUNTIME_ENABLED: bool = False
    PROJECT_SKILL_SNAPSHOT_RETENTION_DAYS: int = 30

    # Citation-faithfulness reviewer pass in draft generation (WS1).
    # Default off: merge inert, flip in values-dev after verify.
    # When flipping on in dev, no secret is needed — boolean env only;
    # if ever sourced from Infisical, add DRAFT_CITATION_REVIEW_ENABLED
    # to the /do-kb path per project convention.
    DRAFT_CITATION_REVIEW_ENABLED: bool = False

    # Option B server-side history rebuild. When True, the agent stream ignores
    # all but the newest user turn in the request and rebuilds conversation
    # context from the LangGraph checkpoint (source of truth), seeding it from
    # the DB when the checkpoint is empty. When False (default), the legacy
    # client-resent-history path is used unchanged. Flag-gated rollout: enable
    # on dev only after soak; the frontend send-only-newest change must NOT ship
    # until this is on in that environment.
    #
    # Requires the client to send a client_message_id on the newest user turn
    # (the /chat surface does when NEXT_PUBLIC_SERVER_CANONICAL_CHAT is on) — it
    # is the idempotency key that keeps two same-content turns distinct and a
    # retry a no-op. Turns without one safely fall back to the legacy path, so
    # enabling this where cmids aren't sent just makes it a no-op, never a bug.
    AGENT_SERVER_SIDE_HISTORY: bool = False

    # Audit P1.3 (X1 dispatch half): where POST /agent/execute runs the turn.
    # "background" (default) keeps today's FastAPI BackgroundTasks path —
    # fire-and-forget on the API pod, lost on pod death. "celery" enqueues the
    # turn to the dedicated `agent_runs` queue after durably committing the
    # agent_runs row (flush-before-external), so a duplicate delivery no-ops
    # on the execution lease and a crashed worker's run is reaped by the
    # sweeper. Values-level rollback: flip back to "background" — no image
    # rebuild. Unknown values degrade to "background" with a warning (a typo
    # in values must not crash the pod at boot).
    AGENT_DISPATCH_BACKEND: str = "background"

    # Execution-lease TTL the Celery runner stamps on the agent_runs row when
    # it claims a job. Must exceed the graph's own 360s hard timeout so a live
    # run can never look lease-expired to the sweeper.
    AGENT_RUN_EXECUTION_LEASE_SECONDS: int = 600

    # Audit P1.4 (X1 recovery half + D7): beat sweepers for stale agent runs
    # and stuck processing jobs. Default on; values-controllable kill switch.
    SWEEPERS_ENABLED: bool = True

    # A non-terminal agent_runs row with no status write for this long is
    # considered dead (graph hard timeout is 360s) and swept to failed.
    AGENT_RUN_STALE_AFTER_SECONDS: int = 1800

    # Live-run heartbeat interval (audit S2-M15): in-process (SSE/background)
    # executions bump agent_runs.updated_at at this cadence so the sweeper's
    # staleness judgment reflects liveness, not a config margin. The sweeper
    # raises its effective cutoff floor to max(STALE_AFTER, 4x this), so a
    # misconfigured STALE_AFTER can never undercut a live heartbeat.
    AGENT_RUN_HEARTBEAT_SECONDS: int = 60

    # awaiting_confirmation is a legitimately-parked state — a user may take
    # a while to confirm. Sweep it only after the Redis job record (TTL 1h)
    # is guaranteed gone and the confirm can no longer succeed anyway.
    AGENT_RUN_STALE_AWAITING_AFTER_SECONDS: int = 7200

    # A non-terminal processing_jobs row with no update for this long is
    # definitively stuck: Celery's hard time limit is 600s, so 30 min of
    # silence means the task was killed/lost without a terminal write (D7 —
    # cleanup_old_jobs only ever deletes terminal rows).
    PROCESSING_JOB_STUCK_AFTER_SECONDS: int = 1800

    # Lost-job reconciler (src/tasks/reconcile_jobs.py). Recovers processing_jobs
    # whose post-commit Celery dispatch never reached the broker (broker outage
    # in the narrow window after the row committed): status=PENDING,
    # celery_task_id IS NULL, untouched for this long. It re-enqueues each such
    # job (attempt tracked on retry_count) and, after MAX_ATTEMPTS re-enqueues,
    # marks it FAILED. Distinct from the stuck-job sweep (which mark-fails
    # already-dispatched jobs stalled mid-flight): the reconciler re-enqueues
    # never-dispatched ones. The AFTER_MINUTES default (10) is well under the
    # stuck threshold (30 min) so recovery runs before the sweep gives up.
    # Its own kill switch so re-enqueue can be disabled independently.
    LOST_JOB_RECONCILER_ENABLED: bool = True
    LOST_JOB_RECONCILE_AFTER_MINUTES: int = 10
    LOST_JOB_RECONCILE_MAX_ATTEMPTS: int = 3
    LOST_JOB_RECONCILE_MAX_PER_RUN: int = 100

    # Audit D5 (P2.5): data-retention beat tasks (src/tasks/retention_tasks.py).
    # Two-stage safety: RETENTION_ENABLED is the kill switch (tasks no-op when
    # false); RETENTION_APPLY is the dry-run gate — false (default) only LOGS
    # what it would delete, true performs the deletes. Both default-safe, so
    # nothing is removed until an operator flips RETENTION_APPLY=true.
    RETENTION_ENABLED: bool = True
    RETENTION_APPLY: bool = False
    # Threads soft-deleted (is_deleted=True) longer than this are hard-deleted
    # (chat_messages + LangGraph checkpoint rows + the thread row).
    RETENTION_SOFT_DELETED_THREAD_DAYS: int = 30
    # Synthetic-traffic checkpoint threads ('synthetic-<key>-<epoch_ms>') older
    # than this — by their embedded timestamp — are purged as machine noise,
    # regardless of soft-delete state. Closes the ~72-threads/day synthetic
    # checkpoint leak.
    RETENTION_SYNTHETIC_THREAD_DAYS: int = 7
    # Append-only analytics_events / audit_events / rag_queries rows older than
    # this are purged (these tables never shrink otherwise). Conservative.
    RETENTION_APPEND_ONLY_DAYS: int = 180
    # Rows touched per table per batch, and max batches per append-only run
    # (bounded work per beat tick).
    RETENTION_BATCH_SIZE: int = 500
    RETENTION_MAX_BATCHES: int = 20
    # Audit P2.3 (D1): scheduled satellite reconciler. Satellite indexing
    # (Neo4j KG / DO KB) is best-effort during ingestion; failures are
    # recorded per-document (neo4j_index_status / do_kb_sync_status =
    # 'failed') and the beat task src.tasks.reconcile_tasks picks them up.
    # Two-stage safety: RECONCILER_ENABLED=true only REPORTS what it would
    # re-drive; actual re-driving additionally requires RECONCILER_APPLY=true
    # (default off — flip in values once report output looks sane).
    RECONCILER_ENABLED: bool = True
    RECONCILER_APPLY: bool = False
    # Rate cap: at most this many documents re-driven (or listed, in
    # report-only mode) per run. Neo4j re-drive re-runs spaCy extraction and
    # DO KB re-drive re-uploads canonical text, so keep runs small.
    RECONCILER_MAX_DOCS_PER_RUN: int = 25
    # Page size for the org-scoped keyset iteration inside one run.
    RECONCILER_BATCH_SIZE: int = 100

    # Per-turn append-only iteration ledger (K-Dense rowan-autosearch
    # pattern). When AGENT_LEDGER_DIR is set, every memory_save_node turn
    # writes runs/<thread_id>/iterations/<turn_n>.json with a full audit
    # record (intent, plan, tool_executions, retrieved_contexts summary,
    # ai_response, reflection_result, tokens, timing). Empty disables.
    AGENT_LEDGER_DIR: Optional[str] = None

    # Re-score DO KB chunks with the Azure Cohere cross-encoder after
    # resolve/filter. DO KB Public Preview returns no scores (we synthesize
    # 1.0-0.05*rank); this replaces them with calibrated relevance. Requires
    # COHERE_RERANK_ENDPOINT + COHERE_RERANK_API_KEY (already provisioned).
    AGENT_DOKB_COHERE_RERANK: bool = True

    # PaperQA2-style gather-evidence inside do_kb_retrieve: rerank-ordered
    # chunks get per-chunk contextual relevance summaries + verbatim quotes
    # from the lightweight LLM, enabling narrow-then-broad iteration within
    # the existing tool-loop ceiling. Adds up to 5 lightweight LLM calls
    # (~20s budget) per do_kb_retrieve call.
    AGENT_ITERATIVE_RETRIEVAL: bool = False

    # Azure AI Cohere Reranking Configuration
    COHERE_RERANK_ENDPOINT: Optional[str] = None
    COHERE_RERANK_API_KEY: Optional[str] = None
    COHERE_RERANK_MODEL: str = "Cohere-rerank-v4.0-pro"
    COHERE_RERANK_TOP_N: int = 10

    # Cohere Embedding Configuration
    COHERE_EMBED_ENDPOINT: Optional[str] = None  # https://api.cohere.com/v2/embed
    COHERE_EMBED_API_KEY: Optional[str] = None  # Falls back to COHERE_RERANK_API_KEY
    COHERE_EMBED_MODEL: str = "embed-v-4-0"  # Azure AI deployment name
    COHERE_EMBED_DIMENSIONS: int = 1024
    COHERE_EMBED_BATCH_SIZE: int = 96

    # Processing Configuration
    MAX_CONCURRENT_JOBS: int = 5
    JOB_RETRY_MAX: int = 3
    JOB_RETRY_DELAY: int = 5  # seconds

    # Search Configuration
    DEFAULT_SEARCH_LIMIT: int = 10
    MAX_SEARCH_LIMIT: int = 50
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Embedding Provider Configuration
    EMBEDDING_PROVIDER: str = (
        "sentence_transformers"  # cohere, sentence_transformers, azure_openai, auto
    )

    # LLM Response Cache Configuration
    LLM_CACHE_ENABLED: bool = True
    LLM_CACHE_TTL_SECONDS: int = 3600  # 1 hour default
    LLM_CACHE_MAX_ENTRIES: int = 5000
    LLM_CACHE_SEMANTIC_ENABLED: bool = True
    LLM_CACHE_SIMILARITY_THRESHOLD: float = 0.92  # 0.0-1.0, higher = stricter matching

    # Thread Context Window Configuration
    # Model-aware defaults for context windows
    MAX_WORKSPACES_PER_ORG: int = 200
    THREAD_DEFAULT_MAX_MESSAGES: int = 20  # Default messages to include in context
    THREAD_DEFAULT_MAX_TOKENS: int = 4000  # Default token limit for context
    THREAD_CONTEXT_WARN_THRESHOLD: float = (
        0.9  # Warn when context usage exceeds this ratio
    )

    # Model-specific token limits (used for model-aware defaults)
    MODEL_CONTEXT_LIMITS: Dict[str, int] = {
        "gpt-3.5-turbo": 16385,
        "gpt-4": 8192,
        "gpt-4-32k": 32768,
        "gpt-4-turbo": 128000,
        "gpt-4o": 128000,
        "claude-3-haiku": 200000,
        "claude-3-sonnet": 200000,
        "claude-3-opus": 200000,
        "claude-3-5-sonnet": 200000,
    }

    # Monitoring
    ENABLE_METRICS: bool = True
    LOG_LEVEL: str = "INFO"

    @field_validator("NEO4J_URI")
    @classmethod
    def validate_neo4j_uri(cls, v):
        if not v.startswith(("bolt://", "neo4j://", "bolt+s://", "neo4j+s://")):
            raise ValueError(
                "Neo4j URI must start with bolt://, neo4j://, bolt+s://, or neo4j+s://"
            )
        return v

    @field_validator("MAX_FILE_SIZE_MB")
    @classmethod
    def validate_max_file_size(cls, v):
        if v <= 0 or v > 1000:  # Max 1GB
            raise ValueError("MAX_FILE_SIZE_MB must be between 1 and 1000")
        return v

    @field_validator("LLM_CACHE_TTL_SECONDS")
    @classmethod
    def validate_cache_ttl(cls, v):
        if v <= 0:
            raise ValueError("LLM_CACHE_TTL_SECONDS must be positive")
        return v

    @field_validator("LLM_CACHE_MAX_ENTRIES")
    @classmethod
    def validate_cache_max_entries(cls, v):
        if v <= 0:
            raise ValueError("LLM_CACHE_MAX_ENTRIES must be positive")
        return v

    @field_validator("LLM_CACHE_SIMILARITY_THRESHOLD")
    @classmethod
    def validate_cache_similarity_threshold(cls, v):
        if v < 0.0 or v > 1.0:
            raise ValueError(
                "LLM_CACHE_SIMILARITY_THRESHOLD must be between 0.0 and 1.0"
            )
        return v

    @field_validator("THREAD_DEFAULT_MAX_MESSAGES")
    @classmethod
    def validate_thread_max_messages(cls, v):
        if v < 1 or v > 1000:
            raise ValueError("THREAD_DEFAULT_MAX_MESSAGES must be between 1 and 1000")
        return v

    @field_validator("THREAD_DEFAULT_MAX_TOKENS")
    @classmethod
    def validate_thread_max_tokens(cls, v):
        if v < 1 or v > 200000:
            raise ValueError("THREAD_DEFAULT_MAX_TOKENS must be between 1 and 200000")
        return v

    @field_validator("THREAD_CONTEXT_WARN_THRESHOLD")
    @classmethod
    def validate_thread_warn_threshold(cls, v):
        if v < 0.0 or v > 1.0:
            raise ValueError(
                "THREAD_CONTEXT_WARN_THRESHOLD must be between 0.0 and 1.0"
            )
        return v

    @field_validator("FREE_TIER_STORAGE_GB")
    @classmethod
    def validate_storage_limit(cls, v):
        if v <= 0 or v > 10000:  # Max 10TB
            raise ValueError("FREE_TIER_STORAGE_GB must be between 1 and 10000")
        return v

    @model_validator(mode="after")
    def _validate_do_kb_required_fields(self):
        """When DO_KB_ENABLED, require token + region + project + embedding model."""
        if not self.DO_KB_ENABLED:
            return self
        missing = [
            name
            for name, value in (
                ("DO_API_TOKEN", self.DO_API_TOKEN),
                ("DO_KB_REGION", self.DO_KB_REGION),
                ("DO_KB_PROJECT_ID", self.DO_KB_PROJECT_ID),
                ("DO_KB_EMBEDDING_MODEL_UUID", self.DO_KB_EMBEDDING_MODEL_UUID),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"DO_KB_ENABLED=True but missing required vars: {', '.join(missing)}"
            )
        if self.DO_KB_PRIMARY_READ and not self.DO_KB_ENABLED:
            raise ValueError("DO_KB_PRIMARY_READ requires DO_KB_ENABLED=True")
        return self

    class Config:
        env_file = "../.env"
        case_sensitive = True
        extra = "ignore"


# Create settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get application settings"""
    return settings
