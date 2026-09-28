"""Owned transport contract for deterministic research-run downloads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine.export_service import (
    ExportArtifact,
    ResearchExportError,
)


def _runs_module() -> Any:
    return import_module("src.api.research_engine.runs")


@pytest.fixture
def export_client(test_app: FastAPI) -> Iterator[tuple[TestClient, Any, AsyncMock]]:
    user = SimpleNamespace(
        id=uuid4(), organization_id=uuid4(), email="owner@example.test"
    )
    db = AsyncMock()
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            yield client, user, db
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize(
    ("format_name", "media_type", "suffix"),
    [
        ("markdown", "text/markdown; charset=utf-8", ".md"),
        ("json", "application/json", ".json"),
        ("csv", "text/csv; charset=utf-8", ".csv"),
    ],
)
def test_owned_completed_run_downloads_each_format_with_safe_headers(
    export_client: tuple[TestClient, Any, AsyncMock],
    format_name: str,
    media_type: str,
    suffix: str,
) -> None:
    """The route must pass authenticated owner identity and safe filenames."""
    client, user, db = export_client
    run_id = uuid4()
    artifact = ExportArtifact(
        content=b"UNVERIFIED audit artifact",
        media_type=media_type,
        filename=f"daily-research-brief-{run_id}{suffix}",
    )
    export = AsyncMock(return_value=artifact)

    with patch.object(_runs_module().ExportService, "export", export):
        response = client.get(
            f"/api/v1/research-engine/runs/{run_id}/export?format={format_name}"
        )

    assert response.status_code == 200
    assert response.content == artifact.content
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{artifact.filename}"'
    )
    export.assert_awaited_once()
    assert export.await_args is not None
    assert export.await_args.args[:3] == (run_id, user.id, format_name)
    assert export.await_args.args[3] is db
    assert "question" not in response.headers["content-disposition"].lower()


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("run_not_found", 404),
        ("run_not_completed", 409),
    ],
)
def test_inaccessible_or_nonterminal_export_returns_stable_safe_error(
    export_client: tuple[TestClient, Any, AsyncMock],
    code: str,
    status_code: int,
) -> None:
    """Same-organization ownership must not widen the owner-only boundary."""
    client, _user, _db = export_client
    run_id = uuid4()
    error = ResearchExportError(
        status_code=status_code,
        code=code,
        message="Run not found" if status_code == 404 else "Run is not completed",
    )

    with patch.object(
        _runs_module().ExportService,
        "export",
        AsyncMock(side_effect=error),
    ):
        response = client.get(
            f"/api/v1/research-engine/runs/{run_id}/export?format=json"
        )

    assert response.status_code == status_code
    assert response.json()["error"]["message"]["code"] == code
    assert "organization" not in response.text.lower()


def test_no_evidence_markdown_has_stable_conflict_but_audit_formats_remain_available(
    export_client: tuple[TestClient, Any, AsyncMock],
) -> None:
    """No-evidence runs have audit data but no reader-facing conclusion."""
    client, _user, _db = export_client
    run_id = uuid4()
    export = AsyncMock(
        side_effect=[
            ResearchExportError(
                status_code=409,
                code="brief_not_available_no_evidence",
                message="No research brief was produced because no evidence remained.",
            ),
            ExportArtifact(
                content=b'{"final_status":"no_evidence"}',
                media_type="application/json",
                filename=f"daily-research-brief-{run_id}.json",
            ),
            ExportArtifact(
                content=b"artifact_status,source_id\r\nno_evidence,\r\n",
                media_type="text/csv; charset=utf-8",
                filename=f"daily-research-brief-{run_id}.csv",
            ),
        ]
    )

    with patch.object(_runs_module().ExportService, "export", export):
        markdown = client.get(
            f"/api/v1/research-engine/runs/{run_id}/export?format=markdown"
        )
        json_response = client.get(
            f"/api/v1/research-engine/runs/{run_id}/export?format=json"
        )
        csv_response = client.get(
            f"/api/v1/research-engine/runs/{run_id}/export?format=csv"
        )

    assert markdown.status_code == 409
    assert (
        markdown.json()["error"]["message"]["code"] == "brief_not_available_no_evidence"
    )
    assert json_response.status_code == 200
    assert b'"final_status":"no_evidence"' in json_response.content
    assert csv_response.status_code == 200
    assert b"no_evidence" in csv_response.content


def test_invalid_export_format_is_rejected_by_schema(
    export_client: tuple[TestClient, Any, AsyncMock],
) -> None:
    client, _user, _db = export_client
    response = client.get(f"/api/v1/research-engine/runs/{uuid4()}/export?format=pdf")

    assert response.status_code == 422
