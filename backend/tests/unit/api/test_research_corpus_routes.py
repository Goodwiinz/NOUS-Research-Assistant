"""Transport contract for GOO-300 search import / corpus export routes."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine import corpus_export
from src.services.research_engine.project_access import ResearchAction

BASE = "/api/v1/research-engine/projects"
DECLARATION = json.dumps({"database": "Embase", "query_text": "aspirin"})


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    actions: list[ResearchAction]
    denied: dict[ResearchAction, int]


def _routes() -> Any:
    return import_module("src.api.research_engine.corpus")


def _receipt(replayed: bool = False) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "kind": "file_import",
        "version": 1,
        "declared": {"database": "Embase"},
        "observed": {"sha256": "a" * 64},
        "parsed_count": 5,
        "accepted_count": 3,
        "rejected_count": 2,
        "replayed": replayed,
        "created_at": datetime.now(timezone.utc),
    }


@pytest.fixture
def harness(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    db = AsyncMock()
    actions: list[ResearchAction] = []
    denied: dict[ResearchAction, int] = {}

    async def fake_resolve(
        _db: object, project_id: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        assert user_id == user.id
        actions.append(action)
        if action in denied:
            raise HTTPException(status_code=denied[action], detail="Project not found")
        return SimpleNamespace(
            collection=SimpleNamespace(id=project_id, name="p"), engine=None
        )

    monkeypatch.setattr(_routes(), "resolve_project", fake_resolve)
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            yield _Harness(
                client=client, user=user, db=db, actions=actions, denied=denied
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def _upload(harness: _Harness, project_id: UUID, data: bytes = b"TY  - X\n") -> Any:
    return harness.client.post(
        f"{BASE}/{project_id}/imports",
        files={"file": ("export.ris", data, "application/x-research-info-systems")},
        data={"format": "ris", "declaration": DECLARATION},
    )


def test_import_is_201_then_replay_is_200_and_commits(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = uuid4()
    service = AsyncMock(
        side_effect=[(_receipt(), True), (_receipt(replayed=True), False)]
    )
    monkeypatch.setattr(_routes(), "import_file", service)

    created = _upload(harness, project_id)
    replayed = _upload(harness, project_id)

    assert created.status_code == 201, created.text
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert harness.actions == [ResearchAction.EDIT, ResearchAction.EDIT]
    assert service.await_args is not None
    assert service.await_args.kwargs["fmt"] == "ris"
    assert service.await_args.kwargs["filename"] == "export.ris"
    assert service.await_args.args[3].query_text == "aspirin"
    assert harness.db.commit.await_count == 2


@pytest.mark.parametrize(("status", "reason"), [(404, "viewer"), (409, "archived")])
def test_import_denied_without_calling_the_service(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, status: int, reason: str
) -> None:
    harness.denied[ResearchAction.EDIT] = status
    service = AsyncMock()
    monkeypatch.setattr(_routes(), "import_file", service)

    response = _upload(harness, uuid4())

    assert response.status_code == status, reason
    service.assert_not_awaited()
    harness.db.commit.assert_not_awaited()


def test_oversized_upload_is_413_before_any_access_or_parse(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_routes().search_import, "MAX_BYTES", 4)
    service = AsyncMock()
    monkeypatch.setattr(_routes(), "import_file", service)

    # Even a caller without EDIT gets 413: the bounded read happens before
    # resolve_project takes any lock, so moving it after the read fails here.
    harness.denied[ResearchAction.EDIT] = 404
    response = _upload(harness, uuid4(), b"12345")

    assert response.status_code == 413
    assert "file_too_large" in response.text
    assert harness.actions == []
    service.assert_not_awaited()


def test_invalid_declaration_is_a_stable_422(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AsyncMock()
    monkeypatch.setattr(_routes(), "import_file", service)

    response = harness.client.post(
        f"{BASE}/{uuid4()}/imports",
        files={"file": ("x.ris", b"x", "text/plain")},
        data={"format": "ris", "declaration": "{not json"},
    )

    assert response.status_code == 422
    assert "invalid_declaration" in response.text
    assert "{not json" not in response.text
    service.assert_not_awaited()


def test_foreign_project_export_is_404(harness: _Harness) -> None:
    harness.denied[ResearchAction.VIEW] = 404

    response = harness.client.get(f"{BASE}/{uuid4()}/corpus/export")

    assert response.status_code == 404


def test_export_is_an_attachment_and_the_zip_unpacks(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = uuid4()
    package = corpus_export.seal(
        {"project": {"collection_id": str(project_id), "name": "p"}}, "now"
    )
    monkeypatch.setattr(_routes(), "build_package", AsyncMock(return_value=package))

    response = harness.client.get(
        f"{BASE}/{project_id}/corpus/export", params={"format": "zip"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith("attachment;")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"corpus.json", "README.txt"}
        assert json.loads(archive.read("corpus.json"))["body_sha256"] == (
            package["body_sha256"]
        )
    # Archived projects stay exportable: export needs only VIEW.
    assert harness.actions == [ResearchAction.VIEW]
    harness.db.commit.assert_not_awaited()


def test_export_rejects_unknown_format(harness: _Harness) -> None:
    response = harness.client.get(
        f"{BASE}/{uuid4()}/corpus/export", params={"format": "ris"}
    )
    assert response.status_code == 422


def test_chase_route_commits_once_and_maps_replay(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AsyncMock(return_value=(_receipt(replayed=True), False))
    monkeypatch.setattr(_routes(), "chase_citations", service)
    project_id, seed = uuid4(), uuid4()

    response = harness.client.post(
        f"{BASE}/{project_id}/citation-chases",
        json={
            "seed_report_id": str(seed),
            "direction": "backward",
            "idempotency_key": "k",
        },
    )

    assert response.status_code == 200, response.text
    assert service.await_args is not None
    assert service.await_args.kwargs["project_id"] == project_id
    assert service.await_args.kwargs["user_id"] == harness.user.id
    harness.db.commit.assert_awaited_once()


def test_coverage_is_view_only_and_never_exhaustive(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.schemas.research_engine import CoverageResponse

    monkeypatch.setattr(
        _routes(),
        "coverage",
        AsyncMock(
            return_value=CoverageResponse(
                found=[], missing=[], searched=[], not_searched=["arxiv"]
            )
        ),
    )

    response = harness.client.post(
        f"{BASE}/{uuid4()}/corpus/coverage", json={"known": [{"doi": "10.1/x"}]}
    )

    assert response.status_code == 200
    assert response.json()["exhaustive"] is False
    assert harness.actions == [ResearchAction.VIEW]
    harness.db.commit.assert_not_awaited()


def test_foreign_receipt_is_404(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _routes(),
        "get_receipt",
        AsyncMock(
            side_effect=HTTPException(status_code=404, detail="Import not found")
        ),
    )

    response = harness.client.get(f"{BASE}/{uuid4()}/imports/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Import not found"
