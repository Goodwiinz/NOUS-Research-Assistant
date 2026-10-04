"""Transport contract for the GOO-311 synthesis routes.

``resolve_project`` is replaced by a fake that applies the real
``_DECISION_ROLE`` map (REVIEW needs REVIEWER) and answers 404 for a foreign
user. Preview runs the real service over a stubbed GOO-310 table built with
``evidence_rules.build_rows`` from the gold fixture.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import synthesis as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole as Role
from src.services.research import claims_service
from src.services.research_engine import evidence_rules as er
from src.services.research_engine import evidence_service, project_access
from src.services.research_engine import synthesis_rules as sr
from src.services.research_engine import synthesis_service as svc
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT = uuid4()
VERSION = uuid4()
TABLE = uuid4()
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
URL = f"/research-engine/projects/{PROJECT}/synthesis"
GOLD = Path(__file__).resolve().parents[2] / "fixtures/synthesis/smd_dl_gold_v1.json"
SELECTION = ("smd_hedges_g", "random_effects_dl", "depressive_symptoms", "12 weeks")
ROLES = {role: uuid5(NAMESPACE_URL, f"goo311-route/{role}") for role in sr.ROLES}


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    roles: set[Role]
    foreign: bool
    db: MagicMock


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    db.commit, db.flush, db.execute = AsyncMock(), AsyncMock(), AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(fields=_fields()))
    h = _Harness(user=SimpleNamespace(id=uuid4()), roles=set(), foreign=False, db=db)

    async def fake_resolve(
        _db: object,
        pid: UUID,
        _user_id: UUID,
        action: ResearchAction = ResearchAction.VIEW,
    ) -> SimpleNamespace:
        if h.foreign:
            raise HTTPException(404, "Project not found")
        required = project_access._DECISION_ROLE.get(action)
        if required is not None and required.isdisjoint(h.roles):
            names = " or ".join(sorted(r.value for r in required))
            raise HTTPException(403, f"{names} role required")
        return SimpleNamespace(
            collection=SimpleNamespace(id=pid),
            effective_roles=frozenset(h.roles),
            organization_id=uuid4(),
        )

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    monkeypatch.setattr(
        svc, "_selection", AsyncMock(return_value=(str(VERSION), SELECTION))
    )
    monkeypatch.setattr(
        evidence_service, "table_version", AsyncMock(return_value=_table())
    )
    monkeypatch.setattr(svc, "_stale_nodes", AsyncMock(return_value=set()))
    monkeypatch.setattr(svc, "_results", AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: h.user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def _fields() -> list[dict[str, Any]]:
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    return [
        {"field_id": str(ROLES[role]), "name": role, **gold["fields"][role]}
        for role in sr.ROLES
    ]


def _table() -> SimpleNamespace:
    """Heterogeneous studies A-D, A reported twice, plus G with sd_c = 0."""
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    case = next(c for c in gold["cases"] if c["id"] == "missing_variance")
    units: dict[str, list[UUID]] = {}
    tips = []
    for study in case["studies"]:
        reports = study["reports"] + (["A-r2"] if study["unit"] == "study:A" else [])
        for report in reports:
            report_id = uuid5(NAMESPACE_URL, report)
            units.setdefault(study["unit"], []).append(report_id)
            for role, value in zip(sr.ROLES, study["arms"]):
                tips.append(
                    er.Tip(
                        accepted_value_id=uuid5(NAMESPACE_URL, f"{report}/{role}"),
                        document_id=uuid5(NAMESPACE_URL, f"doc/{report}"),
                        report_id=report_id,
                        unit=study["unit"],
                        field_id=ROLES[role],
                        source_hash="a" * 64,
                        text_sha256=None,
                        value=value,
                        missingness=None,
                    )
                )
    field_ids = [ROLES[role] for role in sr.ROLES]
    return SimpleNamespace(
        id=TABLE,
        protocol_version_id=VERSION,
        outcome_key="depressive_symptoms",
        timepoint="12 weeks",
        form_version_id=uuid4(),
        field_ids=[str(f) for f in field_ids],
        rows=er.build_rows(units, tips, field_ids, *SELECTION[2:]),
        excluded=[],
        content_hash="c" * 64,
    )


def _query() -> dict[str, str]:
    return {"table_version_id": str(TABLE)} | {k: str(v) for k, v in ROLES.items()}


def test_preview_zero_writes(harness: _Harness) -> None:
    response = harness.client.get(URL + "/preview", params=_query())
    assert response.status_code == 200, response.text
    body = response.json()
    units = [u["unit"] for u in body["included"]]
    assert units == ["study:A", "study:B", "study:C", "study:D"]
    assert len(body["included"][0]["report_ids"]) == 2  # A once, both reports
    assert all("g" not in u or u["g"] is None for u in body["included"])
    assert [(e["unit"], e["reason"]) for e in body["excluded"]] == [
        ("study:G", "invalid_variance:c")
    ]
    assert body["run_failures"] == []
    assert len(body["input_hash"]) == 64
    harness.db.add.assert_not_called()
    harness.db.commit.assert_not_awaited()
    harness.db.flush.assert_not_awaited()


def test_preview_foreign_404(harness: _Harness) -> None:
    harness.foreign = True
    assert harness.client.get(URL + "/preview", params=_query()).status_code == 404
    assert harness.client.get(URL).status_code == 404


def _body(expected: str) -> dict[str, Any]:
    return {
        "table_version_id": str(TABLE),
        "roles": {k: str(v) for k, v in ROLES.items()},
        "expected_input_hash": expected,
        "idempotency_key": "k1",
    }


def test_execute_owner_without_role_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = AsyncMock(side_effect=HTTPException(418, "reached the service"))
    monkeypatch.setattr(svc, "execute", execute)
    response = harness.client.post(URL, json=_body("a" * 64))
    assert response.status_code == 403
    assert response.json()["detail"] == "reviewer role required"
    execute.assert_not_awaited()
    harness.roles = {Role.ADJUDICATOR}  # another decision role does not do
    assert harness.client.post(URL, json=_body("a" * 64)).status_code == 403


def test_execute_rejects_changed_expected_hash(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "lock_aggregate_stream", AsyncMock())
    monkeypatch.setattr(svc, "_replayed_event", AsyncMock(return_value=None))
    harness.roles = {Role.REVIEWER}
    response = harness.client.post(URL, json=_body("b" * 64))
    assert response.status_code == 409
    assert response.json()["detail"] == svc.INPUTS_CHANGED
    harness.db.add.assert_not_called()
    harness.db.commit.assert_not_awaited()


def _result(status: str, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), status=status, supersedes_result_id=None, **extra
    )


async def test_link_to_validation_failed_result_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed, computed, newer = (
        _result("validation_failed"),
        _result("computed"),
        _result("computed"),
    )
    newer.supersedes_result_id = computed.id
    monkeypatch.setattr(
        svc, "_results", AsyncMock(return_value=[failed, computed, newer])
    )
    monkeypatch.setattr(svc, "_stale_nodes", AsyncMock(return_value=set()))
    context: Any = SimpleNamespace(collection=SimpleNamespace(id=PROJECT))
    for row in (failed, computed):  # failed, then superseded
        with pytest.raises(HTTPException) as error:
            await claims_service._synthesis_target(MagicMock(), context, row.id)
        assert (error.value.status_code, error.value.detail) == (
            409,
            svc.RESULT_NOT_CURRENT,
        )
    with pytest.raises(HTTPException) as missing:
        await claims_service._synthesis_target(MagicMock(), context, uuid4())
    assert missing.value.status_code == 404
    target = await claims_service._synthesis_target(MagicMock(), context, newer.id)
    assert target == {"synthesis_result_id": newer.id}
    monkeypatch.setattr(
        svc, "_stale_nodes", AsyncMock(return_value={("synthesis", str(newer.id))})
    )
    with pytest.raises(HTTPException) as stale:
        await claims_service._synthesis_target(MagicMock(), context, newer.id)
    assert stale.value.status_code == 409
