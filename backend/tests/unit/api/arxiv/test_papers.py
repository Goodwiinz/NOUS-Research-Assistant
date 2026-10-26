"""Mounted contract, tenant identity and queue failure handling."""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.arxiv.papers import router
from src.core.database import get_db
from src.core.dependencies import get_current_organization, get_current_user
from src.models.user import User, UserRole

pytestmark = pytest.mark.unit


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, User, object]]:
    from src.services import expensive_work_admission

    monkeypatch.setattr(
        expensive_work_admission, "admit_expensive_work", AsyncMock(return_value=True)
    )
    app = FastAPI()
    app.include_router(router)
    org = SimpleNamespace(id=uuid4())
    user = User(id=uuid4(), role=UserRole.ADMIN, organization_id=org.id)
    db = object()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_organization] = lambda: org
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client, user, db


def test_recent_rejects_non_admin(client: tuple[TestClient, User, object]) -> None:
    http, user, _ = client
    setattr(user, "role", UserRole.USER)
    assert (
        http.post(
            "/api/v1/arxiv/papers/recent", json={"categories": ["cs.AI"]}
        ).status_code
        == 403
    )


def test_queue_failure_does_not_claim_accepted(
    client: tuple[TestClient, User, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.arxiv import recent

    monkeypatch.setattr(
        recent,
        "enqueue_recent",
        AsyncMock(side_effect=RuntimeError("internal broker address")),
    )
    http, _, _ = client
    response = http.post("/api/v1/arxiv/papers/recent", json={"categories": ["cs.AI"]})
    assert response.status_code == 503
    assert "internal broker" not in response.text


def test_recent_uses_authenticated_tenant(
    client: tuple[TestClient, User, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.arxiv import recent

    enqueue = AsyncMock(return_value="task-1")
    monkeypatch.setattr(recent, "enqueue_recent", enqueue)
    http, user, _ = client
    response = http.post(
        "/api/v1/arxiv/papers/recent",
        json={"categories": ["cs.AI"], "max_results": 3, "days": 2},
    )
    assert response.status_code == 202
    assert response.json() == {"task_id": "task-1", "status": "queued"}
    enqueue.assert_awaited_once_with(user.organization_id, user.id, ["cs.AI"], 3, 2)


def test_pdf_redirects_to_authenticated_same_origin_file(
    client: tuple[TestClient, User, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.arxiv import papers

    store = AsyncMock(return_value="document-1")
    monkeypatch.setattr(papers, "store_pdf", store)
    http, user, db = client
    response = http.get(
        "/api/v1/arxiv/papers/pdf",
        params={"paper_id": "math/0309136v1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/api/v1/files/document-1/download"
    assert store.call_args.args == (db, user.organization_id, user.id, "math/0309136v1")
    assert callable(store.call_args.kwargs["admit"])


def test_missing_paper_is_404(
    client: tuple[TestClient, User, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.arxiv import papers

    monkeypatch.setattr(
        papers, "resolve_metadata", AsyncMock(side_effect=papers.PaperNotFound())
    )
    response = client[0].get(
        "/api/v1/arxiv/papers/metadata", params={"paper_id": "2401.00001"}
    )
    assert response.status_code == 404


def test_authentication_required() -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as http:
        assert http.get(
            "/api/v1/arxiv/papers/metadata?paper_id=2401.00001"
        ).status_code in (401, 403)


@pytest.mark.parametrize("path", ["metadata", "pdf"])
def test_cache_miss_admission_failure_stays_429(
    client: tuple[TestClient, User, object], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from src.services import expensive_work_admission
    from src.services.arxiv import papers

    admit = AsyncMock(return_value=False)
    monkeypatch.setattr(expensive_work_admission, "admit_expensive_work", admit)
    monkeypatch.setattr(papers, "_cached", AsyncMock(return_value=None))
    upstream = MagicMock()
    monkeypatch.setattr(papers, "ArXivIngestionService", upstream)
    assert (
        client[0].get(f"/api/v1/arxiv/papers/{path}?paper_id=2401.00001").status_code
        == 429
    )
    admit.assert_awaited_once()
    upstream.assert_not_called()


def test_recent_accepts_physics_categories(
    client: tuple[TestClient, User, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.arxiv import recent

    monkeypatch.setattr(recent, "enqueue_recent", AsyncMock(return_value="task-1"))
    response = client[0].post(
        "/api/v1/arxiv/papers/recent",
        json={"categories": ["physics.optics", "cond-mat.mtrl-sci"]},
    )
    assert response.status_code == 202
