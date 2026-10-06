"""Dependency-aware Kubernetes readiness endpoint."""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

from ..core.config import get_settings
from .checker import HealthChecker, HealthStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

_health_checker: Optional[HealthChecker] = None

# The kubelet polls readiness every 10 seconds. A short cache prevents
# concurrent probes from opening duplicate database and Redis connections.
_readiness_cache_ttl: float = 5.0
_readiness_last_check_time: float = 0.0
_readiness_cached_result: Optional[Dict[str, Any]] = None
_readiness_lock: Optional["asyncio.Lock"] = None

# Starts ready so startup does not fail before main.py validates LLM config.
_llm_config_ready: bool = True


def set_llm_config_ready(ok: bool) -> None:
    """Set whether deployed environments have usable LLM configuration."""
    global _llm_config_ready
    _llm_config_ready = ok


def resolve_startup_readiness(llm_config_ok: bool, is_throwaway_env: bool) -> bool:
    """Block shared environments, but not local/CI boots, on missing LLM config."""
    return llm_config_ok or is_throwaway_env


def get_health_checker() -> HealthChecker:
    """Get the shared checker for readiness-critical dependencies."""
    global _health_checker
    if _health_checker is None:
        settings = get_settings()
        _health_checker = HealthChecker(
            {
                "database_url": settings.DATABASE_URL,
                "redis_url": settings.REDIS_URL,
            }
        )
    return _health_checker


def _get_readiness_lock() -> "asyncio.Lock":
    """Lazily create the readiness lock inside the running event loop."""
    global _readiness_lock
    if _readiness_lock is None:
        _readiness_lock = asyncio.Lock()
    return _readiness_lock


async def _evaluate_readiness() -> Dict[str, Any]:
    """Check only dependencies required for every API request."""
    try:
        checker = get_health_checker()
        checks = (
            ("database", checker.check_database),
            ("redis", checker.check_redis),
        )
        for component, check in checks:
            result = await check()
            if result.status != HealthStatus.HEALTHY:
                return {
                    "ready": False,
                    "detail": f"Critical component {component} is not healthy",
                }
        return {"ready": True, "detail": None}
    except Exception:  # defensive: readiness must fail closed, never 500
        logger.error("Readiness check failed", exc_info=True)
        return {"ready": False, "detail": "Readiness check failed"}


async def get_cached_readiness() -> Dict[str, Any]:
    """Return readiness, evaluating at most once per cache window."""
    global _readiness_last_check_time, _readiness_cached_result

    now = time.time()
    cached = _readiness_cached_result
    if cached is not None and now - _readiness_last_check_time < _readiness_cache_ttl:
        return cached

    async with _get_readiness_lock():
        now = time.time()
        cached = _readiness_cached_result
        if (
            cached is not None
            and now - _readiness_last_check_time < _readiness_cache_ttl
        ):
            return cached

        result = await _evaluate_readiness()
        _readiness_cached_result = result
        _readiness_last_check_time = time.time()
        return result


@router.get("/readiness")
async def readiness_probe():
    """Return 503 until LLM config, PostgreSQL, and Redis are ready."""
    if not _llm_config_ready:
        raise HTTPException(
            status_code=503,
            detail=(
                "LLM configuration incomplete: AZURE_OPENAI_CHAT_ENDPOINT and/or "
                "AZURE_OPENAI_CHAT_API_KEY are not set. Pod kept out of load balancer."
            ),
        )

    readiness = await get_cached_readiness()
    if not readiness["ready"]:
        raise HTTPException(status_code=503, detail=readiness["detail"])

    return {"status": "ready", "timestamp": time.time()}
