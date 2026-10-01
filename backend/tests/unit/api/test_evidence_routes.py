"""Transport contract for the GOO-310 evidence routes.

``resolve_project`` is replaced by a fake that applies the real
``_DECISION_ROLE`` map (REVIEW needs REVIEWER, ADJUDICATE needs ADJUDICATOR)
and answers 404 for a foreign user. Preview and export run the real service
reads over stubbed rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import evidence as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole as Role
from src.services.research import release_rules
from src.services.research_engine import evidence_service as svc
from src.services.research_engine import project_access
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT = uuid4()
VERSION = uuid4()
MATRIX = uuid4()
FORM = uuid4()
FIELD = uuid4()
DOCUMENT = uuid4()
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
URL = f"/research-engine/projects/{PROJECT}/evidence"


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
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: h.user
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def _table(table_id: UUID, supersedes: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=table_id,
        collection_id=PROJECT,
        protocol_version_id=VERSION,
        outcome_key="depressive_symptoms",
        timepoint="12 weeks",
        matrix_id=MATRIX,
        form_version_id=FORM,
        field_ids=[str(FIELD)],
        rows=[
            {
                "row_key": f"report:{DOCUMENT}|depressive_symptoms|12 weeks",
                "unit": f"report:{DOCUMENT}",
                "report_ids": [str(DOCUMENT)],
                "cells": {
                    str(FIELD): {
                        "state": "missing",
                        "value": None,
                        "missingness": None,
                        "tips": [],
                    }
                },
            }
        ],
        excluded=[],
        content_hash=("a" if supersedes is None else "b") * 64,
        created_by_id=uuid4(),
        supersedes_table_id=supersedes,
        created_at=NOW,
    )


def test_resolve_contradiction_reviewer_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = AsyncMock(side_effect=HTTPException(418, "reached the service"))
    monkeypatch.setattr(svc, "record_contradiction", record)
    body = {
        "kind": "resolved",
        "contradiction_id": str(uuid4()),
        "previous_id": str(uuid4()),
        "explanation": "the second report is a subgroup",
        "idempotency_key": "k1",
    }
    harness.roles = {Role.REVIEWER}
    response = harness.client.post(URL + "/contradictions", json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "adjudicator role required"
    record.assert_not_awaited()
    # Dissent takes either role; the role used is the one recorded.
    dissent = {**body, "kind": "dissent"}
    for roles, recorded in (
        ({Role.REVIEWER}, "reviewer"),
        ({Role.ADJUDICATOR}, "adjudicator"),
    ):
        harness.roles = roles
        response = harness.client.post(URL + "/contradictions", json=dissent)
        assert response.status_code == 418  # reached the service
        assert record.await_args is not None
        assert record.await_args.args[3] == recorded
    harness.roles = set()
    response = harness.client.post(URL + "/contradictions", json=dissent)
    assert response.status_code == 403
    assert response.json()["detail"] == "reviewer or adjudicator role required"
    harness.foreign = True
    assert harness.client.post(URL + "/contradictions", json=dissent).status_code == 404
    # An opened row must name its members.
    harness.foreign, harness.roles = False, {Role.REVIEWER}
    opened = {**body, "kind": "opened", "contradiction_id": None, "previous_id": None}
    assert harness.client.post(URL + "/contradictions", json=opened).status_code == 422


def test_certainty_owner_without_role_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    assess = AsyncMock(side_effect=HTTPException(418, "reached the service"))
    monkeypatch.setattr(svc, "assess_certainty", assess)
    body = {
        "table_version_id": str(uuid4()),
        "starting_level": "high",
        "ratings": {"risk_of_bias": None},
        "rationale": "GRADE",
        "idempotency_key": "c1",
    }
    response = harness.client.post(URL + "/certainty", json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "reviewer role required"
    harness.roles = {Role.ADJUDICATOR}  # adjudicating is not assessing
    assert harness.client.post(URL + "/certainty", json=body).status_code == 403
    harness.foreign = True
    assert harness.client.post(URL + "/certainty", json=body).status_code == 404
    harness.foreign, harness.roles = False, {Role.REVIEWER}
    bad = {**body, "ratings": {"risk_of_bias": -3}}
    assert harness.client.post(URL + "/certainty", json=bad).status_code == 422
    confidence = {**body, "ratings": {"confidence": -1}}
    assert harness.client.post(URL + "/certainty", json=confidence).status_code == 422
    assess.assert_not_awaited()
    assert harness.client.post(URL + "/certainty", json=body).status_code == 418


def test_preview_is_read_only_no_commit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _table(uuid4())
    built = svc._Built(
        protocol_version_id=str(VERSION),
        matrix=SimpleNamespace(id=MATRIX),
        form_version_id=FORM,
        field_ids=[FIELD],
        rows=table.rows,
        excluded=[],
        content_hash="a" * 64,
        document_ids=[DOCUMENT],
    )
    cells = [
        {
            "document_id": str(DOCUMENT),
            "field_id": str(FIELD),
            "column_name": "GDS",
            "value": 3.9,
            "missingness": None,
            "source": source,
            "confidence": 0.7,
        }
        for source in ("machine", "accepted")
    ]
    monkeypatch.setattr(svc, "_build", AsyncMock(return_value=built))
    monkeypatch.setattr(svc, "_state", AsyncMock(return_value=svc._State([], [], [])))
    monkeypatch.setattr(svc, "cell_view", AsyncMock(return_value=(None, cells)))
    monkeypatch.setattr(svc, "_suggestions", AsyncMock(return_value=[]))
    harness.roles = set()  # VIEW needs no decision role
    response = harness.client.get(
        URL + "/tables/preview",
        params={
            "outcome_key": "depressive_symptoms",
            "timepoint": "12 weeks",
            "matrix_id": str(MATRIX),
            "field_ids": [str(FIELD)],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"][0]["cells"][str(FIELD)]["state"] == "missing"
    assert [c["source"] for c in body["unreviewed_cells"]] == ["machine"]
    assert body["unreviewed_cells"][0]["review_state"] == "unreviewed"
    assert "confidence" not in body["unreviewed_cells"][0]
    assert body["differs_from_tip"] is False
    harness.db.commit.assert_not_awaited()
    harness.db.flush.assert_not_awaited()
    harness.db.add.assert_not_called()


def test_export_includes_stale_versions(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = uuid4(), uuid4()
    tables = [_table(old), _table(new, supersedes=old)]
    method = svc._Method(
        str(VERSION), {"depressive_symptoms": ("12 weeks",)}, ("grade", "handbook-2013")
    )
    monkeypatch.setattr(svc, "_method", AsyncMock(return_value=method))
    monkeypatch.setattr(
        svc, "_state", AsyncMock(return_value=svc._State(tables, [], []))
    )
    monkeypatch.setattr(
        svc,
        "_stale_nodes",
        AsyncMock(return_value={release_rules.node("evidence_table", old)}),
    )
    monkeypatch.setattr(svc, "_names", AsyncMock(return_value={}))
    response = harness.client.get(URL + "/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        f'attachment; filename="evidence-{PROJECT}.json"'
    )
    package: dict[str, Any] = response.json()
    assert package["schema"] == svc.EXPORT_SCHEMA
    (outcome,) = package["body"]["outcomes"]
    assert [(t["id"], t["stale"], t["superseded"]) for t in outcome["tables"]] == [
        (str(old), True, True),
        (str(new), False, False),
    ]
    assert package["body"]["certainty_method"]["domains"][0] == "risk_of_bias"
    harness.db.commit.assert_not_awaited()
