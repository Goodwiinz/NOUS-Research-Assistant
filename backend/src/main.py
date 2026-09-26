"""
Main FastAPI application for the multimodal RAG system
"""

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import redis  # Added this line
import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.gzip import GZipMiddleware

# Setup basic logging
logger = logging.getLogger(__name__)

from src.api.agent import agent_router
from src.api.arxiv import (
    arxiv_bulk_router,
    arxiv_change_router,
    arxiv_extraction_router,
    arxiv_kg_router,
    arxiv_llm_bulk_router,
    arxiv_local_router,
    arxiv_router,
)
from src.api.auth import auth_router, cli_auth_router
from src.api.auth.api_keys import router as api_keys_router
from src.api.connectors import connectors_router
from src.api.diagnostics import diagnostics_router
from src.api.documents import (
    documents_router,
    figures_router,
    files_router,
    integrity_router,
    processing_router,
    table_extraction_router,
)
from src.api.evidence.router import router as evidence_router
from src.api.infrastructure import evaluation_router, workers_router
from src.api.quality import (
    performance_dashboard_router,
    quality_metrics_router,
    quality_recommendations_router,
    user_behavior_router,
)
from src.api.realtime import (
    realtime_quality_metrics_router,
    realtime_status_router,
    websocket_v2_router,
)
from src.api.research import (
    chat_router,
    citations_router,
    drafts_router,
    export_router,
    extraction_matrix_router,
    pipeline_router,
    project_chat_router,
    project_report_router,
    project_skills_router,
    projects_router,
    tone_engine_router,
    writer_router,
)
from src.api.research_engine import (
    research_engine_blueprints_router,
    research_engine_projects_router,
    research_engine_runs_router,
    research_engine_steps_router,
)
from src.api.search import knowledge_graph_router, search_quality_router, search_router
from src.api.security import compliance_router, encryption_router, rbac_router
from src.api.threads import (
    thread_search_router,
    threads_router,
    workspaces_router,
    workspaces_standalone_router,
)
from src.core.config import settings
from src.core.database import Base, engine
from src.core.probes import ProbeAwareTrustedHostMiddleware
from src.core.security import auth_rate_limiter
from src.exceptions import RAGException
from src.exceptions.analytics_exceptions import AnalyticsException
from src.exceptions.error_handlers import (
    analytics_exception_handler,
    database_exception_handler,
    rag_exception_handler,
)
from src.health.endpoints import router as health_router
from src.middleware.disconnect_signal import AgentDisconnectSignalMiddleware
from src.middleware.multi_tenancy import MultiTenancyMiddleware
from src.middleware.rate_limiting import AnalyticsRateLimitMiddleware
from src.middleware.security_headers import SecurityHeadersMiddleware

# from src.services.documents.file_service import redis_client  # Not exported, not needed here

# Initialize Sentry early so the SDK can patch frameworks before app creation.
# No-op when SENTRY_DSN is unset.
try:
    from src.observability.sentry import init_sentry

    init_sentry()
except Exception as _sentry_err:  # noqa: BLE001
    print(f"Warning: Sentry init failed: {_sentry_err}")

# Configure observability (optional)
try:
    from src.observability import (
        configure_logging,
        configure_metrics,
        configure_tracing,
        instrument_app,
        instrument_services,
    )

    configure_logging()
    configure_tracing()
    configure_metrics()
    OBSERVABILITY_ENABLED = True
except ImportError as e:
    print(f"Warning: Observability not available: {e}")
    OBSERVABILITY_ENABLED = False

    # Create dummy functions
    def instrument_app(app):
        return app

    def instrument_services():
        pass


# Global Redis client instance
redis_client: Optional[redis.Redis] = None

try:
    redis_client = redis.from_url(settings.REDIS_URL)
    # We don't ping here to avoid blocking startup if Redis is down,
    # but the client object is created so middleware can use it (and fail gracefully later).
except Exception as e:
    logger.warning(
        f"Failed to create Redis client: {e}. Rate limiting will use in-memory storage."
    )
    redis_client = None


# Export Celery broker queue depth as a Prometheus gauge (celery_queue_depth) on
# the global registry — see observability/celery_queue_metrics.py and audit item
# P1.6. This is the backlog signal a future KEDA/prometheus-adapter-driven worker
# HPA will scale on; HPA wiring itself is deferred.
try:
    from src.observability.celery_queue_metrics import (
        register_celery_queue_depth_collector,
    )

    register_celery_queue_depth_collector(settings.REDIS_URL)
except Exception as _queue_metric_err:  # noqa: BLE001
    print(
        f"Warning: Celery queue-depth metric registration failed: {_queue_metric_err}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    logger.info("Starting up Multimodal RAG System...")

    # Create database tables ONLY for a genuinely local dev DB. Any managed /
    # deployed database (dev/staging/prod all set SUPABASE_DB_URL) is owned by the
    # Alembic init container. Running create_all against a managed DB (a) grabs
    # session-mode pooler connections on every worker boot and can exhaust
    # Supabase's pool, and (b) races Alembic: create_all skips existing tables and
    # never back-fills constraints added to a model later, permanently drifting the
    # schema (this is how project_threads lost uq_project_thread). Fail-safe:
    # require BOTH an explicit development ENVIRONMENT and the absence of
    # SUPABASE_DB_URL, so a managed cluster can never trigger create_all even if
    # ENVIRONMENT is misconfigured to "development".
    # Third condition (audit M1): the engine host must be genuinely local (or
    # SQLite, or explicitly forced via RUN_CREATE_ALL=1). Closes the residual
    # gap where ENVIRONMENT is unset (defaults "development") and a managed
    # non-Supabase DATABASE_URL is configured — that combination previously
    # still ran create_all against the managed DB.
    from src.core.database import should_run_create_all

    environment = os.environ.get("ENVIRONMENT", "development")
    supabase_db_url = os.environ.get("SUPABASE_DB_URL", "")
    db_host = getattr(engine.url, "host", None)
    is_sqlite = engine.url.get_backend_name().startswith("sqlite")
    force_create_all = os.environ.get("RUN_CREATE_ALL", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if should_run_create_all(
        environment, supabase_db_url, db_host, is_sqlite, force_create_all
    ):
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("Database tables created successfully")
        except Exception as e:
            if "already exists" in str(e):
                logger.info("Database tables already exist")
            else:
                logger.error(f"Failed to create database tables: {e}")
                raise
    else:
        logger.info(
            "Skipping create_all (environment=%s, supabase_db_url_set=%s, "
            "db_host=%s, forced=%s) — Alembic migrations are authoritative",
            environment,
            bool(supabase_db_url),
            db_host,
            force_create_all,
        )

    # Initialize field-level encryption (requires ENCRYPTION_MASTER_KEY env var).
    # Non-fatal: if the key is missing the app still boots. Plaintext/legacy rows
    # read back raw; genuinely-encrypted values that can't be decrypted return
    # None (never the ciphertext) — see EncryptedType.process_result_value.
    try:
        from src.core.encryption import initialize_encryption

        initialize_encryption()
        logger.info("Field-level encryption initialized")
    except Exception as e:
        logger.info("Encryption initialization skipped: %s", e)

    # Check Redis connection
    if redis_client:
        try:
            redis_client.ping()  # Test connection
            logger.info("Redis client connected successfully")
        except redis.RedisError as e:
            logger.warning(
                f"Redis connection failed: {e}. Rate limiting may not persist across restarts."
            )

    # Initialize WebSocket services
    try:
        from src.services.websocket.websocket_service_initializer import (
            websocket_service_initializer,
        )

        await websocket_service_initializer.initialize()
        logger.info("WebSocket services initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize WebSocket services: {e}")
        # Continue startup even if WebSocket services fail

    # Validate S3 storage backend if configured
    from src.core.config import settings as app_settings

    if app_settings.STORAGE_BACKEND == "s3":
        from src.core.s3_client import S3StorageHelper, missing_s3_credentials

        missing = missing_s3_credentials()
        if missing:
            raise RuntimeError(
                f"STORAGE_BACKEND=s3 but missing required env vars: {', '.join(missing)}"
            )
        try:
            helper = S3StorageHelper()
            if not helper.check_health():
                logger.warning("S3 storage health check failed — uploads may fail")
            else:
                logger.info(
                    "S3 storage backend verified",
                    extra={"bucket": app_settings.S3_BUCKET_NAME},
                )
        except Exception as e:
            raise RuntimeError(f"S3 storage backend initialization failed: {e}")

    # Configure LangSmith tracing for agent observability
    try:
        from src.services.agent.observability import configure_langsmith

        configure_langsmith()
    except Exception as e:
        logger.debug(f"LangSmith configuration skipped: {e}")

    # Validate LLM (Azure/OpenAI) configuration at startup.
    # In strict (shared/deployed) environments, a missing endpoint/key flips
    # the readiness flag to not-ready so the pod is kept out of the load
    # balancer rather than crashing on every agent turn. In a throwaway
    # local/CI boot (``is_throwaway_environment``) it is a warning only and
    # MUST NOT block readiness — otherwise a dev-labelled process with
    # missing/rotated Azure config would flip every pod NotReady while the
    # log claims readiness isn't blocked. Uses the same throwaway-env
    # predicate as ``require_durable_agent_state`` so the two gates can't drift.
    try:
        from src.health.endpoints import resolve_startup_readiness, set_llm_config_ready
        from src.services.agent.llm_factory import validate_llm_config

        llm_config_ok = validate_llm_config()
        is_throwaway = app_settings.is_throwaway_environment
        set_llm_config_ready(resolve_startup_readiness(llm_config_ok, is_throwaway))
        if llm_config_ok:
            logger.info("LLM config validated OK")
        elif is_throwaway:
            # Throwaway boot: keep readiness ready, warn only.
            logger.warning(
                "LLM config incomplete: AZURE_OPENAI_CHAT_ENDPOINT and/or "
                "AZURE_OPENAI_CHAT_API_KEY (or non-CHAT variants) are unset. "
                "Agent turns will fail at call time. "
                "Continuing in %s without blocking readiness.",
                environment,
            )
        else:
            # Strict env: readiness already flipped False so the pod leaves the LB.
            logger.critical(
                "LLM config incomplete: AZURE_OPENAI_CHAT_ENDPOINT and "
                "AZURE_OPENAI_CHAT_API_KEY (or AZURE_OPENAI_ENDPOINT / "
                "AZURE_OPENAI_API_KEY) must be set. Readiness probe will "
                "return 503 until resolved (environment=%s).",
                environment,
            )
    except Exception as e:
        logger.debug("LLM config validation skipped: %s", e)

    # Initialise LangGraph checkpointer + memory store at startup so the
    # first request doesn't pay the setup() cost (and so a misconfigured
    # Postgres connection surfaces immediately wherever durable agent
    # state is required — i.e. every env except local/CI throwaways).
    try:
        from src.services.agent.checkpointer import get_checkpointer
        from src.services.agent.memory import get_memory_store

        await asyncio.gather(get_checkpointer(), get_memory_store())
        logger.info("LangGraph checkpointer + memory store warmed at startup")
    except Exception as e:
        if settings.require_durable_agent_state:
            logger.error(
                "LangGraph persistence warm-up failed and durable agent "
                "state is required (ENVIRONMENT=%s) — aborting startup. "
                "Set ALLOW_MEMORY_FALLBACK=true to permit the non-durable "
                "in-memory fallback instead. Cause: %s",
                environment,
                e,
            )
            raise
        logger.warning(
            "LangGraph persistence warm-up skipped (in-memory fallback "
            "permitted): %s",
            e,
        )

    # Pre-populate critical caches in the background (non-blocking)
    try:
        from src.core.cache_warmup import warm_critical_caches

        asyncio.create_task(warm_critical_caches())
    except Exception as e:
        logger.debug(f"Cache warm-up skipped: {e}")

    logger.info("Application startup complete")

    from src.services.agent.typesafe_classifier import (
        close_typesafe_client,
        start_typesafe_client,
    )

    start_typesafe_client()
    try:
        yield
    finally:
        try:
            await close_typesafe_client()
        except Exception as exc:
            logger.warning("TypeSafe client shutdown failed (%s)", type(exc).__name__)

    # Shutdown
    logger.info("Shutting down Multimodal RAG System...")

    # Close Redis client
    if redis_client:
        try:
            redis_client.close()
            logger.info("Redis client closed successfully")
        except redis.RedisError as e:
            logger.error(f"Error closing Redis client: {e}")

    # Close LangGraph persistence pools (checkpointer + memory store)
    try:
        from src.services.agent._pool_utils import close_shared_langgraph_pool
        from src.services.agent.checkpointer import close_checkpointer
        from src.services.agent.memory import close_memory_store

        # Drop singleton references first so no in-flight handle keeps
        # the pool busy when we close it.
        await asyncio.gather(
            close_checkpointer(), close_memory_store(), return_exceptions=True
        )
        await close_shared_langgraph_pool()
        logger.info("LangGraph persistence pools closed")
    except Exception as e:
        logger.warning("Error closing LangGraph persistence pools: %s", e)

    # Close auth rate limiter
    try:
        await auth_rate_limiter.close()
        logger.info("Auth rate limiter closed successfully")
    except Exception as e:
        logger.error(f"Error closing auth rate limiter: {e}")

    # Shutdown WebSocket services
    try:
        from src.services.websocket.websocket_service_initializer import (
            websocket_service_initializer,
        )

        await websocket_service_initializer.shutdown()
        logger.info("WebSocket services shutdown successfully")
    except Exception as e:
        logger.error(f"Error shutting down WebSocket services: {e}")

    # Shutdown agent job store Redis connection
    try:
        from src.services.agent.job_store import close_redis

        await close_redis()
        logger.info("Agent job store Redis connection closed")
    except Exception as e:
        logger.error(f"Error closing agent job store Redis: {e}")


class SelectiveGZipMiddleware(GZipMiddleware):
    """Compress JSON/text responses, but pass SSE/streaming + file-export
    routes through UNCOMPRESSED. gzip buffers to accumulate before flushing,
    which would stall token-by-token SSE (the whole point of streaming) — so
    any path containing ``/stream`` (agent stream / confirm / resume, thread
    stream) or ending in ``/export`` bypasses compression. Everything else
    (list_messages, list_threads, thread-detail, search JSON) gets 70-85% off
    the wire size."""

    async def __call__(self, scope, receive, send):  # type: ignore[override]
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if "/stream" in path or path.endswith("/export"):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description="A multimodal enterprise RAG system for intelligent document search and analysis",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    redirect_slashes=False,  # Prevent 307 redirects that strip Authorization headers
)

# Instrument application with observability
instrument_app(app)

# Instrument additional services
if OBSERVABILITY_ENABLED:
    try:
        from src.services.documents.file_service import redis_client

        instrument_services(sql_engine=engine, redis_client=redis_client)
    except ImportError:
        instrument_services(sql_engine=engine)

# Add CORS middleware
# SECURITY: Strict CORS configuration - only allow specified origins, headers, and methods
# Never use allow_origins=["*"] or allow_headers=["*"] in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
    allow_credentials=True,
    allow_methods=settings.cors_methods_list,
    allow_headers=settings.cors_headers_list,
    expose_headers=settings.cors_expose_list,
    max_age=settings.CORS_MAX_AGE,  # Cache preflight for 24 hours
)

# Compress JSON/text responses (list_messages/list_threads/thread-detail/
# search) — SSE + export routes are excluded so token streaming isn't buffered.
app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)

# Add rate limiting middleware for analytics endpoints
app.add_middleware(AnalyticsRateLimitMiddleware, redis_client=redis_client)

# Add multi-tenancy middleware — runs before rate limiting so tenant context is
# available when rate limit decisions are made (registered after = executes first).
app.add_middleware(MultiTenancyMiddleware)

# Add trusted host middleware for production.
# Kubelet HTTP probes set Host header to the pod IP, which is not in the
# allow-list — that produced HTTP 400 on /health and crash-looped pods.
# Probe paths (incl. /health/readiness) are exempted from host validation via
# PROBE_EXEMPT_PATHS; see src/core/probes.py. A missing readiness exemption
# would 400 every readiness probe and flap all pods out of the LB.
# The allow-list is settings.TRUSTED_HOSTS (comma-separated) so an environment
# can add the hostname it is actually reached on — a server-side Next.js rewrite
# forwards the DESTINATION host, which is how CI's `backend` service name ends up
# in the Host header.
if not settings.DEBUG:
    app.add_middleware(
        ProbeAwareTrustedHostMiddleware,
        allowed_hosts=settings.trusted_hosts_list,
    )


# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add response time header"""
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log incoming requests"""
    start_time = time.time()

    # Log request
    logger.info(
        f"Request: {request.method} {request.url.path} "
        f"from {request.client.host if request.client else 'unknown'}"
    )

    response = await call_next(request)

    # Log response
    process_time = time.time() - start_time
    logger.info(f"Response: {response.status_code} in {process_time:.4f}s")

    return response


# Security response headers (audit #10). Outermost response-mutating middleware:
# its headers apply to every response, including those short-circuited by inner
# middleware (CORS preflight, rate-limit 429, trusted-host 400).
app.add_middleware(SecurityHeadersMiddleware)
# Transparent outer receive wrapper: every downstream middleware shares its
# disconnect signal, while SecurityHeadersMiddleware still mutates responses.
app.add_middleware(AgentDisconnectSignalMiddleware)


# Include routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(cli_auth_router, prefix="/api/v1")
app.include_router(api_keys_router, prefix="/api/v1")
app.include_router(files_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(processing_router, prefix="/api/v1")
app.include_router(knowledge_graph_router, prefix="/api/v1")
app.include_router(search_router, prefix="/api/v1")
app.include_router(search_quality_router, prefix="/api/v1")
app.include_router(evidence_router, prefix="/api/v1/evidence", tags=["evidence"])
app.include_router(quality_metrics_router, prefix="/api/v1/analytics/quality")
app.include_router(user_behavior_router, prefix="/api/v1/analytics/behavior")
app.include_router(performance_dashboard_router, prefix="/api/v1/analytics/performance")
app.include_router(
    quality_recommendations_router, prefix="/api/v1/analytics/recommendations"
)
app.include_router(workers_router, prefix="/api/v1")
app.include_router(encryption_router, prefix="/api/v1/security")
app.include_router(compliance_router, prefix="/api/v1/security")
app.include_router(rbac_router, prefix="/api/v1/rbac")
app.include_router(evaluation_router, prefix="/api/v1")
app.include_router(
    diagnostics_router, prefix="/api/v1"
)  # Retrieval diagnostics endpoints
app.include_router(websocket_v2_router)  # Enhanced WebSocket v2 routes
app.include_router(realtime_status_router)  # Real-time document status API
app.include_router(
    realtime_quality_metrics_router, prefix="/api/v2"
)  # Real-time quality metrics API
app.include_router(arxiv_router)  # ArXiv integration endpoints
app.include_router(
    arxiv_kg_router, prefix="/api/v1/arxiv/kg"
)  # ArXiv Knowledge Graph endpoints
app.include_router(
    arxiv_change_router, prefix="/api/v1/arxiv/tracking"
)  # ArXiv Change Tracking endpoints
app.include_router(
    arxiv_extraction_router, prefix="/api/v1/arxiv/extraction"
)  # ArXiv Feature Extraction endpoints
app.include_router(
    arxiv_local_router, prefix="/api/v1/arxiv/local"
)  # Local ArXiv PDF processing endpoints
app.include_router(
    arxiv_bulk_router, prefix="/api/v1"
)  # Kaggle bulk ingestion endpoints
app.include_router(
    arxiv_llm_bulk_router, prefix="/api/v1"
)  # LLM-powered bulk ingestion with embeddings
app.include_router(chat_router, prefix="/api/v1")  # Chat completion endpoints
app.include_router(agent_router)  # Agent execution endpoints
app.include_router(connectors_router)  # External database connectors
app.include_router(
    workspaces_router
)  # Thread-centric workspace/conversation/thread/message API
# IMPORTANT: threads_router MUST be included BEFORE workspaces_standalone_router
# because threads_router has /bulk/* routes that need to match before /{thread_id}
app.include_router(
    threads_router, prefix="/api/v2"
)  # Thread management endpoints (includes bulk operations)
app.include_router(
    workspaces_standalone_router
)  # Flat API routes for workspaces (used by frontend)
app.include_router(export_router, prefix="/api/v1")  # Thread export endpoints
app.include_router(citations_router)  # Research Assistant citations endpoints
app.include_router(projects_router)  # Research Assistant projects endpoints
app.include_router(project_report_router)  # GET /api/v1/projects/{id}/report.html
app.include_router(project_skills_router)  # Project skill catalog and staged approvals
app.include_router(project_chat_router)  # Project-Chat integration endpoints
app.include_router(drafts_router)  # Research Assistant drafts endpoints
app.include_router(tone_engine_router)  # Scholarly Tone Engine endpoints
app.include_router(extraction_matrix_router)  # Extraction Matrix endpoints
app.include_router(writer_router)  # AI Writer endpoints
app.include_router(pipeline_router)  # Research Pipeline wizard endpoints
app.include_router(integrity_router)  # AI Integrity Detector endpoints
app.include_router(table_extraction_router)  # Table & math extraction endpoints
app.include_router(figures_router)  # Extracted figures endpoints
app.include_router(
    research_engine_projects_router, prefix="/api/v1"
)  # Research Engine projects
app.include_router(
    research_engine_blueprints_router, prefix="/api/v1"
)  # Research Engine blueprints
app.include_router(
    research_engine_runs_router, prefix="/api/v1"
)  # Research Engine runs
app.include_router(
    research_engine_steps_router, prefix="/api/v1"
)  # Research Engine steps
app.include_router(
    thread_search_router, prefix="/api/v2"
)  # Thread and message full-text search

# Include dependency-aware readiness endpoint
app.include_router(health_router)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.VERSION,
        "docs_url": (
            "/docs" if settings.DEBUG else "Documentation not available in production"
        ),
        "health_check": "/health",
    }


# Global exception handlers
def _sanitize_log(value: Any) -> str:
    """Neutralize CR/LF in request-derived values before logging.

    Prevents log-forging / line-injection when a request path or error detail
    contains newlines. (JSON log output already escapes these, but plaintext
    handlers and downstream consumers should not have to rely on that.)
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle validation errors"""
    # 422 is a client error (malformed request), not a server fault — log at
    # warning so it doesn't inflate error-rate alerts (matches the HTTP 4xx
    # handling below).
    logger.warning(
        "Validation error on %s: %s",
        _sanitize_log(request.url.path),
        _sanitize_log(exc.errors()),
    )
    safe_errors = []
    for err in exc.errors():
        safe = {k: v for k, v in err.items() if k != "ctx"}
        if "ctx" in err and isinstance(err["ctx"], dict):
            safe["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
        safe_errors.append(safe)
    first_msg = (
        safe_errors[0].get("msg", "Validation error")
        if safe_errors
        else "Validation error"
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "message": first_msg,
                "status_code": 422,
                "type": "validation_error",
                "details": safe_errors,
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle HTTP exceptions.

    Log by severity class: 5xx are server faults (ERROR); 4xx are routine
    client conditions — expired/missing JWTs (401), missing rows (404),
    validation (422) — and must NOT be logged at ERROR, or they flood the
    error logs and inflate error-rate alerts with normal traffic. The auth
    layer already records the expired-token case at DEBUG.
    """
    status_code = getattr(exc, "status_code", 500)
    log = logger.error if status_code >= 500 else logger.warning
    log(
        "HTTP %s error on %s: %s",
        status_code,
        _sanitize_log(request.url.path),
        _sanitize_log(exc.detail),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "status_code": exc.status_code,
                "type": "http_error",
            }
        },
        headers=dict(exc.headers) if exc.headers else None,
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions"""
    logger.error("Unhandled exception: %s", exc, exc_info=True)

    message = "Internal server error"
    # DEBUG (not raw ENVIRONMENT) gates the leak: config.py's
    # _enforce_debug_off_in_prod force-clears DEBUG in production AND
    # staging, so this can never fire outside a genuine local/dev DEBUG
    # run. The old `ENVIRONMENT not in ("production",)` check leaked full
    # exception details in staging/dev deployments, where ENVIRONMENT is
    # "staging"/"development" but the service is still reachable.
    if settings.DEBUG:
        message = f"{type(exc).__name__}: {exc}"

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "message": message,
                "status_code": 500,
                "type": "internal_error",
            }
        },
    )


# Wire the application's own exception hierarchies to their structured handlers.
# Registered explicitly rather than via
# src.exceptions.error_handlers.setup_error_handlers(), which would also
# re-register HTTPException / RequestValidationError / Exception and clobber the
# handlers defined above. Starlette resolves handlers by walking the exception's
# MRO, so these more-specific handlers take precedence over the generic
# Exception handler for their own types — e.g. an analytics
# PermissionDeniedException now returns 403 instead of a generic 500, and a
# SQLAlchemyError returns a sanitized 500 instead of leaking DB internals in
# non-production environments.
app.add_exception_handler(RAGException, rag_exception_handler)
app.add_exception_handler(AnalyticsException, analytics_exception_handler)
app.add_exception_handler(SQLAlchemyError, database_exception_handler)


# Development server info — requires admin auth even in DEBUG mode
if settings.DEBUG:
    from src.core.dependencies import require_admin

    @app.get("/debug/info", dependencies=[Depends(require_admin)])
    async def debug_info():
        """Debug information endpoint (admin-only, DEBUG mode only)"""
        return {
            "settings": {
                "environment": settings.ENVIRONMENT,
                "log_level": settings.LOG_LEVEL,
                "enable_metrics": settings.ENABLE_METRICS,
                "max_file_size_mb": settings.MAX_FILE_SIZE_MB,
                "free_tier_storage_gb": settings.FREE_TIER_STORAGE_GB,
            },
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
