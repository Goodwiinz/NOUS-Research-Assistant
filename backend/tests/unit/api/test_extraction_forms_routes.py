"""Transport contract for GOO-304 extraction form, observation and accept routes.

Mutation check (docs/testing/agent-orchestration-mutation-checks.md, GOO-304):
dropping the ``ResearchProjectRole.ADJUDICATOR`` check at the top of
``extraction_forms_service.accept_value`` makes
``test_accept_requires_adjudicator`` fail (the service no longer answers 403).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole
from src.services.research import extraction_forms_service as svc
from src.services.research import extraction_rules as rules
from src.services.research_engine.project_access import ResearchAction
from src.shared.scispace_schemas import ExtractionAcceptCreate

pytestmark = pytest.mark.unit

BASE = "/api/v1/research/matrices"
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
_REQUIRED = {
    ResearchAction.REVIEW: ResearchProjectRole.REVIEWER,
    ResearchAction.ADJUDICATE: ResearchProjectRole.ADJUDICATOR,
}


def _routes() -> Any:
    return import_module("src.api.research.extraction_matrix")


def _result(scalar: Any = None, rows: list[Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalars.return_value.all.return_value = rows or []
    return result


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    roles: set[ResearchProjectRole]
    project_id: UUID


@pytest.fixture
def harness(
    test_app: FastAPI,
    test_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    db = AsyncMock()
    db.add = MagicMock()
    roles: set[ResearchProjectRole] = set()
    project_id = uuid4()

    async def fake_resolve(
        _db: object, pid: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        """Like resolve_project: a decision action needs its role (403)."""
        assert user_id == user.id and pid == project_id
        required = _REQUIRED.get(action)
        if required is not None and required not in roles:
            raise HTTPException(403, f"{required.value} role required")
        return SimpleNamespace(
            collection=SimpleNamespace(id=pid), effective_roles=frozenset(roles)
        )

    monkeypatch.setattr(_routes(), "resolve_project", fake_resolve)
    monkeypatch.setattr(svc, "matrix_project_id", AsyncMock(return_value=project_id))
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(
            test_app, headers=test_auth_headers, raise_server_exceptions=False
        ) as client:
            yield _Harness(
                client=client, user=user, db=db, roles=roles, project_id=project_id
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def _observe_body() -> dict[str, Any]:
    return {
        "document_id": str(uuid4()),
        "field_id": str(uuid4()),
        "form_version_id": str(uuid4()),
        "value": "12",
        "idempotency_key": "k1",
    }


def _accept_body(**overrides: Any) -> dict[str, Any]:
    return {
        "document_id": str(uuid4()),
        "field_id": str(uuid4()),
        "form_version_id": str(uuid4()),
        "observation_ids": [str(uuid4())],
        "value": 12,
        "rationale": "matches the paper",
        "idempotency_key": "a1",
        **overrides,
    }


def test_observe_requires_reviewer_role_owner_gets_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    observe = AsyncMock()
    monkeypatch.setattr(svc, "observe", observe)
    # A workspace owner with no research role holds no implicit REVIEW power.
    response = harness.client.post(
        f"{BASE}/{uuid4()}/observations", json=_observe_body()
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "reviewer role required"
    observe.assert_not_awaited()


def test_accept_requires_adjudicator(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = {
        "id": str(uuid4()),
        "form_version_id": str(uuid4()),
        "field_id": str(uuid4()),
        "document_id": str(uuid4()),
        "value": 12,
        "missingness": None,
        "observation_ids": [str(uuid4())],
        "accepted_by_id": str(harness.user.id),
        "rationale": "r",
        "source_hash": "a" * 64,
        "supersedes_accepted_value_id": None,
        "created_at": NOW.isoformat(),
    }
    accept = AsyncMock(return_value=accepted)
    monkeypatch.setattr(svc, "accept_value", accept)
    harness.roles.add(ResearchProjectRole.REVIEWER)
    response = harness.client.post(
        f"{BASE}/{uuid4()}/accepted-values", json=_accept_body()
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "adjudicator role required"
    accept.assert_not_awaited()

    harness.roles.add(ResearchProjectRole.ADJUDICATOR)
    response = harness.client.post(
        f"{BASE}/{uuid4()}/accepted-values", json=_accept_body()
    )
    assert response.status_code == 201
    assert response.json()["id"] == accepted["id"]


async def test_accept_service_requires_adjudicator_without_the_route() -> None:
    """The service refuses on its own, before touching the database."""
    db = AsyncMock()
    context: Any = SimpleNamespace(
        collection=SimpleNamespace(id=uuid4()),
        effective_roles=frozenset({ResearchProjectRole.REVIEWER}),
    )
    with pytest.raises(HTTPException) as error:
        await svc.accept_value(
            db,
            context,
            uuid4(),
            uuid4(),
            ExtractionAcceptCreate.model_validate(_accept_body()),
        )
    assert error.value.status_code == 403
    db.execute.assert_not_awaited()


def test_accept_rejects_value_not_in_citations_422(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.roles.add(ResearchProjectRole.ADJUDICATOR)
    version_id, document_id = uuid4(), uuid4()
    (field,) = rules.build_fields(uuid4(), [{"name": "N", "type": "number"}])
    source = "a" * 64
    cited = SimpleNamespace(
        id=uuid4(),
        kind="human",
        value=12,
        missingness=None,
        validation_state="valid",
        form_version_id=version_id,
        citation=None,
        source_hash=source,
        created_at=NOW,
    )
    monkeypatch.setattr(svc, "_matrix", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(svc, "_lock", AsyncMock())
    monkeypatch.setattr(svc, "_replayed_event", AsyncMock(return_value=None))
    monkeypatch.setattr(
        svc,
        "_document",
        AsyncMock(
            return_value=SimpleNamespace(
                id=document_id, checksum_sha256=source, content_text=None
            )
        ),
    )
    monkeypatch.setattr(
        svc,
        "_current_field",
        AsyncMock(return_value=(SimpleNamespace(id=version_id), field)),
    )
    harness.db.execute.side_effect = [_result(None), _result(rows=[cited])]
    body = _accept_body(
        document_id=str(document_id),
        field_id=field["field_id"],
        form_version_id=str(version_id),
        observation_ids=[str(cited.id)],
        value=13,
    )
    response = harness.client.post(f"{BASE}/{uuid4()}/accepted-values", json=body)
    assert response.status_code == 422
    assert "equal one valid cited observation" in response.json()["error"]["message"]
    harness.db.add.assert_not_called()
    harness.db.commit.assert_not_awaited()


def test_patch_same_columns_creates_no_version(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = SimpleNamespace(
        id=uuid4(),
        project_id=harness.project_id,
        name="m",
        columns=[{"name": "N", "description": None}],
        updated_at=None,
    )
    columns = [{"name": "N", "type": "number"}]
    fields = rules.build_fields(matrix.id, columns)
    current = SimpleNamespace(
        id=uuid4(), content_hash=rules.form_hash("authored", None, fields)
    )
    monkeypatch.setattr(
        svc, "current_protocol_version_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(svc, "_lock", AsyncMock())
    monkeypatch.setattr(svc, "current_version", AsyncMock(return_value=current))
    harness.db.execute.return_value = _result(matrix)

    response = harness.client.patch(f"{BASE}/{matrix.id}", json={"columns": columns})

    assert response.status_code == 200, response.text
    assert response.json()["columns_changed"] is False
    harness.db.add.assert_not_called()
    harness.db.commit.assert_awaited_once()


def test_patch_rejects_categorical_without_categories(harness: _Harness) -> None:
    response = harness.client.patch(
        f"{BASE}/{uuid4()}", json={"columns": [{"name": "Arm", "type": "categorical"}]}
    )
    assert response.status_code == 422


def test_get_matrix_keeps_legacy_keys(harness: _Harness) -> None:
    document_id = uuid4()
    matrix = SimpleNamespace(
        id=uuid4(),
        project_id=harness.project_id,
        name="m",
        columns=[{"name": "Design", "description": None}],
        created_at=NOW,
        updated_at=NOW,
    )
    cell = SimpleNamespace(
        document_id=document_id,
        column_name="Design",
        value="RCT",
        citation_snippet="p3",
        confidence=0.8,
    )
    harness.db.execute.side_effect = [
        _result(matrix),  # the matrix
        _result(rows=[document_id]),  # allowed project documents
        _result(rows=[cell]),  # frozen legacy cells
        _result(rows=[]),  # no form version row (raw-SQL fixture)
    ]

    response = harness.client.get(f"{BASE}/{matrix.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["form_version"] is None
    assert body["columns"] == matrix.columns
    (only,) = body["cells"]
    assert only == {
        "document_id": str(document_id),
        "column_name": "Design",
        "value": "RCT",
        "citation_snippet": "p3",
        "confidence": 0.8,
        "field_id": None,
        "form_version_id": None,
        "source": "legacy",
        "missingness": None,
        "validation_state": None,
        "stale": False,
        # GOO-305: computed, never stored; the legacy 0.8 was never measured.
        "anchor_status": "legacy_unanchored",
        "confidence_calibration": "uncalibrated",
    }
