"""GOO-305 transport contract: anchor refusals, the source-changed commit and
archived-project reads, through the real routes and the real forms service
(its lookups patched, the session faked)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from src.models.extraction_matrix import ExtractionObservation
from src.models.research_project_role import ResearchProjectRole
from src.services.research import extraction_forms_service as svc
from src.services.research import source_anchors as anchors
from src.services.research_engine.project_access import ResearchAction
from tests.unit.api.test_extraction_forms_routes import (  # noqa: F401
    BASE,
    NOW,
    _Harness,
    _result,
    _routes,
    harness,
)

pytestmark = pytest.mark.unit

TEXT = "Arms were randomized. Arms were randomized. We enrolled 40 adults."
FIELD_ID = uuid4()
FIELD = {"field_id": str(FIELD_ID), "name": "Design", "type": "text"}


@pytest.fixture
def cell(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """One document and form version reachable through the patched lookups."""
    document = SimpleNamespace(id=uuid4(), content_text=TEXT, checksum_sha256="a" * 64)
    version = SimpleNamespace(id=uuid4(), fields=[FIELD])
    matrix = SimpleNamespace(id=uuid4(), project_id=harness.project_id)
    monkeypatch.setattr(svc, "_matrix", AsyncMock(return_value=matrix))
    monkeypatch.setattr(svc, "_lock", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(svc, "_replayed_event", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_document", AsyncMock(return_value=document))
    monkeypatch.setattr(svc, "_current_field", AsyncMock(return_value=(version, FIELD)))
    monkeypatch.setattr(svc, "_append", AsyncMock())
    monkeypatch.setattr(svc, "_invalidate_releases", AsyncMock())  # GOO-307
    monkeypatch.setattr(svc, "_versions", AsyncMock(return_value=[version]))
    monkeypatch.setattr(svc, "_staled_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(svc, "_tips", AsyncMock(return_value=[]))
    return SimpleNamespace(document=document, version=version, matrix=matrix)


def _row(cell: SimpleNamespace, quote: str, pins: tuple[str, str] | None = None) -> Any:
    source_hash, text_hash = pins or svc.document_pins(cell.document)
    return ExtractionObservation(
        id=uuid4(),
        form_version_id=cell.version.id,
        field_id=FIELD_ID,
        document_id=cell.document.id,
        kind="machine",
        actor_user_id=uuid4(),
        value="RCT",
        missingness=None,
        validation_state="valid",
        citation=quote,
        source_hash=source_hash,
        text_sha256=text_hash,
        created_at=NOW,
        **svc._anchor_columns(anchors.verify_anchor(TEXT, quote)),
    )


def _post_accept(
    harness: _Harness, cell: SimpleNamespace, cited: Any, **body: Any
) -> Any:
    harness.db.execute.side_effect = [_result(None), _result(rows=[cited])]
    return harness.client.post(
        f"{BASE}/{cell.matrix.id}/accepted-values",
        json={
            "document_id": str(cell.document.id),
            "field_id": str(FIELD_ID),
            "form_version_id": str(cell.version.id),
            "observation_ids": [str(cited.id)],
            "value": "RCT",
            "rationale": "methods section",
            "idempotency_key": "k",
            **body,
        },
    )


def test_anchor_409_details_are_stable_strings(
    harness: _Harness, cell: SimpleNamespace
) -> None:
    harness.roles.add(ResearchProjectRole.ADJUDICATOR)
    ambiguous = _row(cell, "Arms were randomized.")
    response = _post_accept(harness, cell, ambiguous)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == svc.ANCHOR_AMBIGUOUS
    unverified = _row(cell, "Arms were rand0mized.")
    response = _post_accept(harness, cell, unverified)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == svc.ANCHOR_UNVERIFIED
    response = _post_accept(harness, cell, unverified, anchor_start=-1)
    assert response.status_code == 422
    harness.db.add.assert_not_called()
    harness.db.commit.assert_not_awaited()


def test_source_changed_commits_exactly_once(
    harness: _Harness, cell: SimpleNamespace
) -> None:
    harness.roles.add(ResearchProjectRole.ADJUDICATOR)
    stale = _row(cell, "We enrolled 40 adults.", pins=("a" * 64, "0" * 64))
    response = _post_accept(harness, cell, stale)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == svc.SOURCE_CHANGED
    assert harness.db.commit.await_count == 1
    harness.db.add.assert_not_called()


def test_archived_project_reads_observations_but_refuses_accept(
    harness: _Harness, cell: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def archived(
        _db: object, pid: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        """Like resolve_project on an archived Collection."""
        if action is not ResearchAction.VIEW:
            raise HTTPException(409, "Archived projects are read-only")
        return SimpleNamespace(
            collection=SimpleNamespace(id=pid), effective_roles=frozenset()
        )

    monkeypatch.setattr(_routes(), "resolve_project", archived)
    row = _row(cell, "We enrolled 40 adults.")
    harness.db.execute.side_effect = [_result(rows=[row]), _result(rows=[])]
    response = harness.client.get(
        f"{BASE}/{cell.matrix.id}/observations",
        params={"document_id": str(cell.document.id), "field_id": str(FIELD_ID)},
    )
    assert response.status_code == 200
    (observation,) = response.json()["observations"]
    assert observation["anchor"]["status"] == "verified"
    assert observation["context_after"] == ""
    assert observation["source_changed"] is False
    response = _post_accept(harness, cell, row)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Archived projects are read-only"
