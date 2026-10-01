"""Transport contract for the GOO-309 appraisal routes.

``resolve_project`` is replaced by a fake that applies the real
``_DECISION_ROLE`` map (REVIEW needs REVIEWER, ADJUDICATE needs ADJUDICATOR)
and answers 404 for a foreign user. The list runs the real reveal logic over
stubbed rows.
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

from src.api.research_engine import appraisals as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole as Role
from src.services.research_engine import appraisal_rules
from src.services.research_engine import appraisal_service as svc
from src.services.research_engine import project_access
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT = uuid4()
STUDY = uuid4()
VERSION = uuid4()
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
URL = f"/research-engine/projects/{PROJECT}/appraisals"


def _row(assessor: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        collection_id=PROJECT,
        protocol_version_id=VERSION,
        instrument_key="rob2",
        instrument_version="2019-08-22",
        instrument_spec_hash=appraisal_rules.SPEC_HASH,
        study_id=STUDY,
        report_id=None,
        target_key=f"study:{STUDY}",
        outcome_key="depressive_symptoms",
        timepoint="12 weeks",
        study_design="randomized_parallel_group",
        applicability="applicable",
        domains=appraisal_rules.normalize({}),
        overall=None,
        kind="independent",
        actor_role="reviewer",
        assessor_id=assessor,
        resolves_assessment_ids=None,
        rationale=None,
        input_hash="a" * 64,
        supersedes_assessment_id=None,
        created_at=NOW,
    )


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    roles: set[Role]
    foreign: bool
    rows: list[Any]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    h = _Harness(user=SimpleNamespace(id=uuid4()), roles=set(), foreign=False, rows=[])

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
            collection=SimpleNamespace(id=pid), effective_roles=frozenset(h.roles)
        )

    method = svc._Method(
        str(VERSION),
        ("rob2", "2019-08-22", "dual_independent"),
        {"depressive_symptoms": ("12 weeks",)},
    )
    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    monkeypatch.setattr(svc, "_method", AsyncMock(return_value=method))
    monkeypatch.setattr(svc, "_rows", AsyncMock(side_effect=lambda *_: h.rows))
    monkeypatch.setattr(svc, "_units", AsyncMock(return_value={f"study:{STUDY}"}))
    monkeypatch.setattr(svc, "_evidence_options", AsyncMock(return_value={}))
    monkeypatch.setattr(svc, "_stale_nodes", AsyncMock(return_value=set()))
    monkeypatch.setattr(svc, "_names", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: h.user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def _submit_body() -> dict[str, Any]:
    return {
        "protocol_version_id": str(VERSION),
        "instrument_key": "rob2",
        "instrument_version": "2019-08-22",
        "study_id": str(STUDY),
        "outcome_key": "depressive_symptoms",
        "timepoint": "12 weeks",
        "study_design": "randomized_parallel_group",
        "applicability": "applicable",
        "domains": {"D1": {"judgment": None}},
        "idempotency_key": "k1",
    }


def test_submit_owner_without_role_403_viewer_404(harness: _Harness) -> None:
    response = harness.client.post(URL, json=_submit_body())
    assert response.status_code == 403
    assert response.json()["detail"] == "reviewer role required"
    harness.roles = {Role.ADJUDICATOR}  # adjudicating is not reviewing
    assert harness.client.post(URL, json=_submit_body()).status_code == 403
    harness.foreign = True
    assert harness.client.post(URL, json=_submit_body()).status_code == 404
    assert harness.client.get(URL).status_code == 404
    harness.foreign, harness.roles = False, {Role.REVIEWER}
    both = {**_submit_body(), "report_id": str(uuid4())}
    assert harness.client.post(URL, json=both).status_code == 422


def test_adjudicate_reviewer_403(harness: _Harness) -> None:
    harness.roles = {Role.REVIEWER}
    body = {
        **_submit_body(),
        "resolves_assessment_ids": [str(uuid4())],
        "rationale": "resolved",
    }
    response = harness.client.post(URL + "/adjudications", json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "adjudicator role required"


def test_list_hides_unrevealed_peer_rows_and_counts(harness: _Harness) -> None:
    peer = _row(uuid4())
    harness.rows = [peer]
    response = harness.client.get(URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instrument"]["key"] == "rob2"
    assert "riskofbias" in body["instrument"]["licence"]
    (result,) = body["results"]
    assert result["status"] == "awaiting_independent"
    assert result["rows"] == [] and result["mine"] is False
    assert result["unresolved_domains"] == []
    assert str(peer.id) not in response.text
    assert str(peer.assessor_id) not in response.text
    assert "count" not in response.text
    # The same response whether or not a peer submitted.
    harness.rows = []
    assert harness.client.get(URL).json() == body
    # The assessor sees their own row; the second assessor reveals both.
    mine = _row(harness.user.id)
    harness.rows = [mine]
    (result,) = harness.client.get(URL).json()["results"]
    assert [r["id"] for r in result["rows"]] == [str(mine.id)]
    assert result["mine"] is True and result["status"] == "awaiting_independent"
    harness.rows = [peer, mine]
    (result,) = harness.client.get(URL).json()["results"]
    assert {r["id"] for r in result["rows"]} == {str(peer.id), str(mine.id)}
    assert result["status"] == "agreed"


def test_export_content_disposition(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = {"schema": svc.EXPORT_SCHEMA, "body_sha256": "0" * 64, "body": {}}
    export = AsyncMock(return_value=package)
    monkeypatch.setattr(svc, "export_package", export)
    response = harness.client.get(URL + "/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="appraisal-{PROJECT}.json"'
    )
    assert response.json() == package
    assert export.await_args is not None
    assert export.await_args.args[2] == harness.user.id  # never viewer-less
