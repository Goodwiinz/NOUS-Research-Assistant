"""Audit I17: unauthenticated ``GET /health`` and ``GET /`` must not disclose
the deployed VERSION or ENVIRONMENT (fingerprinting aid).

Mutation check (docs/engineering/testing.md):
- file:function: backend/src/main.py:health_check (and :root)
- mutation: add ``"version": settings.VERSION, "environment": settings.ENVIRONMENT``
  back to the ``health_check`` dict (or ``"version": settings.VERSION`` to ``root``)
- run: ``pytest -q backend/tests/unit/api/test_public_endpoints_no_version_leak.py``
- expected: the key-absence asserts and the sentinel tripwire fail.
"""

import json

import pytest

from src.main import health_check, root, settings

pytestmark = pytest.mark.unit

_VERSION_SENTINEL = "9.9.9-i17-sentinel"
_ENV_SENTINEL = "i17-sentinel-env"


@pytest.fixture(autouse=True)
def _sentinel_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VERSION", _VERSION_SENTINEL)
    monkeypatch.setattr(settings, "ENVIRONMENT", _ENV_SENTINEL)


@pytest.mark.asyncio
async def test_health_reports_healthy_without_version_or_environment() -> None:
    body = await health_check()

    assert body["status"] == "healthy"
    assert "timestamp" in body
    assert "version" not in body
    assert "environment" not in body
    text = json.dumps(body)
    assert _VERSION_SENTINEL not in text
    assert _ENV_SENTINEL not in text


@pytest.mark.asyncio
async def test_root_does_not_disclose_version_or_environment() -> None:
    body = await root()

    assert "version" not in body
    assert body["health_check"] == "/health"
    text = json.dumps(body)
    assert _VERSION_SENTINEL not in text
    assert _ENV_SENTINEL not in text
