"""GOO-319: citation chasing runs only when the protocol requires it, through
the run's OpenAlex connector (never a fresh network client)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services.research_engine import corpus_service
from src.services.research_engine import search_update_service as svc


class _OpenAlex:
    async def citations(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _db() -> MagicMock:
    db = MagicMock()
    db.commit, db.rollback = AsyncMock(), AsyncMock()
    return db


def _job() -> dict[str, Any]:
    return {"collection_id": uuid4(), "owner_id": uuid4(), "execution_id": uuid4()}


async def test_chase_not_required_calls_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        corpus_service, "_protocol_requirement", AsyncMock(return_value=("v", None))
    )
    chase = AsyncMock()
    monkeypatch.setattr(corpus_service, "chase_citations", chase)
    result = await svc._chase(_db(), _job(), [str(uuid4())], {})
    assert result == {"required": False, "status": "not_required"}
    chase.assert_not_awaited()


async def test_required_chase_uses_run_connector_and_caps_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = {"required": True, "directions": ["backward", "sideways"]}
    monkeypatch.setattr(
        corpus_service,
        "_protocol_requirement",
        AsyncMock(return_value=("v", requirement)),
    )
    chase = AsyncMock(return_value=(SimpleNamespace(id=uuid4()), True))
    monkeypatch.setattr(corpus_service, "chase_citations", chase)
    openalex = _OpenAlex()
    seeds = sorted(str(uuid4()) for _ in range(svc.MAX_CHASE_SEEDS + 2))
    job = _job()
    result = await svc._chase(_db(), job, seeds, {"openalex": openalex})
    assert result["directions"] == ["backward"]
    assert result["seeds_over_cap"] == 2
    assert chase.await_count == svc.MAX_CHASE_SEEDS
    kwargs = chase.await_args_list[0].kwargs
    assert kwargs["connector"] is openalex
    assert kwargs["data"].idempotency_key == (
        f"scheduled:{job['execution_id']}:{seeds[0]}:backward"
    )
    assert all("receipt_id" in c for c in result["chases"])


async def test_duplicate_report_import_checks_every_accepted_doi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins metadata must not discard an earlier observation's DOI."""
    db, job = _db(), _job()
    report_id, receipt_id = uuid4(), uuid4()
    first = {"title": "Earlier", "identifiers": {"doi": "10.1/EARLIER"}}
    latest = {"title": "Latest", "identifiers": {"doi": "10.1/latest"}}
    rows = [
        SimpleNamespace(report_id=report_id, parsed=first),
        SimpleNamespace(report_id=report_id, parsed=latest),
    ]
    monkeypatch.setattr(svc, "_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(svc, "_start", AsyncMock(return_value=("running", job)))
    monkeypatch.setattr(
        svc,
        "_recheck",
        AsyncMock(return_value=(None, SimpleNamespace(organization_id=uuid4()))),
    )
    monkeypatch.setattr(
        svc,
        "_receipt",
        AsyncMock(return_value=SimpleNamespace(id=receipt_id, observed={})),
    )
    monkeypatch.setattr(svc, "_baseline", AsyncMock(return_value=(None, {})))
    monkeypatch.setattr(svc, "_chase", AsyncMock(return_value={}))
    finish = AsyncMock(return_value="succeeded")
    monkeypatch.setattr(svc, "_finish", finish)
    notice = {"notice_doi": "10.1/earlier.retraction", "type": "retraction"}
    crossref = SimpleNamespace(
        update_notices=AsyncMock(
            return_value={"10.1/earlier": [notice], "10.1/latest": []}
        )
    )

    result = await svc.run_execution(
        db, job["execution_id"], lambda _organization_id: {"crossref": crossref}
    )

    assert result == "succeeded"
    crossref.update_notices.assert_awaited_once_with(["10.1/earlier", "10.1/latest"])
    assert finish.await_args.args[5] == {"10.1/earlier": [notice], "10.1/latest": []}
    records, _dois = await svc._current(db, receipt_id)
    assert records == {str(report_id): latest}
