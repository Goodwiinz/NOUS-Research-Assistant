"""Transport contract for the GOO-312 run-manifest, artifact and figure routes.

``require_run`` is replaced by a fake that answers 404 for a foreign user;
the service runs for real over a mocked session and in-memory storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import experiments as routes
from src.api.research_engine import runs as run_routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.artifacts.storage import MemoryArtifactStorage
from src.services.research_engine import experiment_service as svc
from src.services.research_engine import manifest_rules

pytestmark = pytest.mark.unit

RUN = uuid4()
MANIFEST = {"schema": manifest_rules.SCHEMA, "run_id": str(RUN), "seed": 7}


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    foreign: bool
    run: SimpleNamespace
    manifest_row: Any
    storage: MemoryArtifactStorage


def _result(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(None))
    h = _Harness(
        db=db,
        foreign=False,
        run=SimpleNamespace(
            id=RUN,
            status="completed",
            reproducibility_manifest={"_pause_requested": True, "total_tokens": 3},
        ),
        manifest_row=None,
        storage=MemoryArtifactStorage(),
    )

    async def fake_require_run(_db: object, run_id: UUID, *_: object) -> Any:
        if h.foreign or run_id != RUN:
            raise HTTPException(404, "Run not found")
        return h.run

    async def fake_manifest_row(_db: object, _run_id: object) -> Any:
        return h.manifest_row

    monkeypatch.setattr(routes, "require_run", fake_require_run)
    monkeypatch.setattr(svc, "_manifest_row", fake_manifest_row)
    monkeypatch.setattr(svc, "get_artifact_storage", lambda: h.storage)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    h.client = TestClient(app)
    yield h


def test_manifest_download_bytes_match_hash(harness: _Harness) -> None:
    digest = manifest_rules.manifest_hash(MANIFEST)
    harness.manifest_row = SimpleNamespace(
        manifest=MANIFEST,
        manifest_hash=digest,
        completeness="incomplete",
        missing=["code.sha256"],
    )
    response = harness.client.get(f"/research-engine/runs/{RUN}/manifest/v2/download")
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == digest
    assert response.headers["X-Content-SHA256"] == digest
    body = harness.client.get(f"/research-engine/runs/{RUN}/manifest/v2").json()
    assert body["schema"] == manifest_rules.SCHEMA
    assert (body["completeness"], body["missing"]) == ("incomplete", ["code.sha256"])
    # A legacy run has no v2 bytes to download.
    harness.manifest_row = None
    missing = harness.client.get(f"/research-engine/runs/{RUN}/manifest/v2/download")
    assert missing.status_code == 404


def test_artifact_route_cross_project_404(harness: _Harness) -> None:
    data = b"<svg/>"
    digest = hashlib.sha256(data).hexdigest()
    artifact = SimpleNamespace(
        id=uuid4(),
        sha256=digest,
        media_type="image/svg+xml",
        storage_key=f"artifacts/org/research-runs/{RUN}/{digest}",
    )
    asyncio.run(harness.storage.put(artifact.storage_key, data, "image/svg+xml"))
    harness.db.execute = AsyncMock(return_value=_result(artifact))
    url = f"/research-engine/runs/{RUN}/artifacts/{artifact.id}"
    response = harness.client.get(url)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["X-Content-SHA256"] == digest
    assert artifact.storage_key not in json.dumps(dict(response.headers))
    # The artifact query is run-scoped: another run's artifact is not found.
    harness.db.execute = AsyncMock(return_value=_result(None))
    assert harness.client.get(url).status_code == 404
    # A foreign user never reaches the run.
    harness.db.execute = AsyncMock(return_value=_result(artifact))
    harness.foreign = True
    assert harness.client.get(url).status_code == 404
    assert (
        harness.client.get(f"/research-engine/runs/{RUN}/manifest/v2").status_code
        == 404
    )


def test_no_storage_key_in_any_response_model(harness: _Harness) -> None:
    spec = json.dumps(harness.client.app.openapi())  # type: ignore[attr-defined]
    assert "storage_key" not in spec
    assert "FigureLineageResponse" in spec and "RunArtifactResponse" in spec


def test_legacy_manifest_route_unchanged(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def owned_run(*_: object, **__: object) -> Any:
        return harness.run

    monkeypatch.setattr(run_routes, "_get_owned_run", owned_run)
    legacy = asyncio.run(
        run_routes.get_manifest(RUN, SimpleNamespace(id=uuid4()), harness.db)
    )
    body = harness.client.get(f"/research-engine/runs/{RUN}/manifest/v2").json()
    assert body["schema"] == manifest_rules.LEGACY_SCHEMA
    assert (body["completeness"], body["missing"]) == (
        "incomplete",
        ["schema_version<2"],
    )
    assert body["legacy"] == legacy == {"total_tokens": 3, "run_status": "completed"}
    assert body["manifest"] is None and body["manifest_hash"] is None
    for key in ("code", "environment", "inputs", "outputs"):
        assert key not in body and key not in body["legacy"]
