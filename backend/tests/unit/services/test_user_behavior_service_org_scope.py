"""Org scope + cache isolation for UserBehaviorService.analyze_user_behavior (GOO-407).

analyze_user_behavior must filter SearchSession by the caller's organization and
key its cache by (org, user, days_back) so a cached result is never served across
tenants. asyncio_mode=auto, so plain ``async def`` tests run natively.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.quality import user_behavior_service as svc_mod
from src.services.quality.user_behavior_service import UserBehaviorService

pytestmark = pytest.mark.unit

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER = "11111111-1111-1111-1111-111111111111"


def _install_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    monkeypatch.setattr(svc_mod, "get_db_sync", lambda: iter([db]))
    return db


async def test_session_query_filters_by_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _install_db(monkeypatch)
    service = UserBehaviorService()

    metrics = await service.analyze_user_behavior(USER, 30, organization_id=ORG_A)

    assert metrics.session_count == 0
    clauses = db.query.return_value.filter.call_args.args
    org_clauses = [
        c
        for c in clauses
        if getattr(getattr(c, "left", None), "key", None) == "organization_id"
    ]
    assert len(org_clauses) == 1
    assert org_clauses[0].right.value == uuid.UUID(ORG_A)


async def test_null_org_returns_empty_without_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_db = MagicMock()
    monkeypatch.setattr(svc_mod, "get_db_sync", get_db)
    service = UserBehaviorService()

    metrics = await service.analyze_user_behavior(USER, 30, organization_id=None)

    assert metrics.session_count == 0
    assert metrics.top_queries == []
    get_db.assert_not_called()


async def test_cache_key_contains_org_and_is_not_served_cross_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = UserBehaviorService()
    cached: Any = SimpleNamespace(marker="org-A result")
    service.behavior_cache[f"{ORG_A}_{USER}_30"] = cached
    # A legacy tenant-less key must never be served either.
    service.behavior_cache[f"{USER}_30"] = SimpleNamespace(marker="legacy")

    db = _install_db(monkeypatch)
    other = await service.analyze_user_behavior(USER, 30, organization_id=ORG_B)
    assert other is not cached
    assert getattr(other, "marker", None) is None
    db.query.assert_called()  # org B was a cache miss and hit the DB

    db_a = _install_db(monkeypatch)
    same = await service.analyze_user_behavior(USER, 30, organization_id=ORG_A)
    assert same is cached
    db_a.query.assert_not_called()


async def test_generate_report_passes_org_into_user_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = UserBehaviorService()
    metrics = service._create_empty_behavior_metrics(USER)
    analyze = AsyncMock(return_value=metrics)
    monkeypatch.setattr(service, "analyze_user_behavior", analyze)
    monkeypatch.setattr(service, "get_behavior_trends", AsyncMock(return_value={}))
    monkeypatch.setattr(
        service, "identify_behavior_patterns", AsyncMock(return_value={})
    )

    report = await service.generate_behavior_report(
        user_id=USER, organization_id=ORG_A, days_back=14
    )

    analyze.assert_awaited_once_with(USER, 14, organization_id=ORG_A)
    assert report["user_metrics"]["user_id"] == USER
    assert "organization_metrics" in report  # org-level part unchanged
