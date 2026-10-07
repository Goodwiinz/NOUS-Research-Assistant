"""Security regressions for ingestion endpoints (found via ingestion bug-hunt).

1. The Kaggle bulk-ingest routers (arxiv_bulk, arxiv_llm_bulk) are global,
   process-wide and cost-intensive. Tenant ADMIN roles must not grant access;
   both routers carry a router-level platform-operator dependency.
2. The research-engine rag_store connector searched the hybrid index with no
   organization_id, so a run surfaced (and copied full_text from) every
   tenant's documents. The run owner's org is now threaded into the search.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.database import get_db
from src.core.security import TokenData
from src.models.user import User, UserRole
from src.services.connectors.base import (
    ConnectorCapability,
    ConnectorDomain,
    ConnectorInfo,
    ConnectorResult,
)

pytestmark = pytest.mark.unit


def _dep_funcs(router):
    return {getattr(d.dependency, "__name__", "") for d in router.dependencies}


def test_bulk_routers_require_platform_operator_at_router_level():
    from src.core import dependencies as dependency_module

    require_platform_operator = getattr(
        dependency_module, "require_platform_operator", None
    )
    assert (
        require_platform_operator is not None
    ), "bulk ingestion must use the platform operator boundary"
    from src.api.arxiv.arxiv_bulk import router as bulk
    from src.api.arxiv.arxiv_llm_bulk import router as llm_bulk

    assert require_platform_operator.__name__ in _dep_funcs(
        bulk
    ), "arxiv_bulk router must require a platform operator"
    assert require_platform_operator.__name__ in _dep_funcs(
        llm_bulk
    ), "arxiv_llm_bulk router must require a platform operator"


def test_bulk_routers_do_not_use_tenant_admin_dependency():
    from src.api.arxiv.arxiv_bulk import router as bulk
    from src.api.arxiv.arxiv_llm_bulk import router as llm_bulk
    from src.core.dependencies import require_admin

    assert require_admin.__name__ not in _dep_funcs(bulk)
    assert require_admin.__name__ not in _dep_funcs(llm_bulk)


def test_rag_store_search_passes_org_to_hybrid_search():
    import asyncio

    from src.api.research_engine import runs as runs_mod

    captured = {}

    class _Resp:
        results = []

    def _fake_search(search_request, user_id=None, organization_id=None):
        captured["organization_id"] = organization_id
        return _Resp()

    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        new=_fake_search,
    ):
        asyncio.run(runs_mod._search_rag_store("q", organization_id="org-123"))

    assert captured["organization_id"] == "org-123", (
        "rag_store search must be scoped to the run owner's org, not run "
        "unfiltered across all tenants"
    )


def test_build_connectors_binds_org_into_rag_store():
    from src.api.research_engine import runs as runs_mod

    connectors = runs_mod._build_connectors(organization_id="org-abc")
    search_fn = connectors["rag_store"].search_fn
    # functools.partial binds organization_id as a keyword.
    assert getattr(search_fn, "keywords", {}).get("organization_id") == "org-abc"


# I28: use isolated routers, with no MultiTenancyMiddleware or auth overrides.
# JWT verification and external effects are mocked; the real bearer/current-user
# and platform-operator dependencies execute against a local SQLite user row.
_GLOBAL_ENDPOINTS = [
    ("arxiv.arxiv_bulk", "POST", "/start"),
    ("arxiv.arxiv_bulk", "GET", "/status"),
    ("arxiv.arxiv_bulk", "POST", "/stop"),
    ("arxiv.arxiv_bulk", "GET", "/stats"),
    ("arxiv.arxiv_bulk", "POST", "/test-small-batch"),
    ("arxiv.arxiv_llm_bulk", "POST", "/start"),
    ("arxiv.arxiv_llm_bulk", "GET", "/status"),
    ("arxiv.arxiv_llm_bulk", "POST", "/stop"),
    ("arxiv.arxiv_llm_bulk", "GET", "/stats"),
    ("arxiv.arxiv_llm_bulk", "GET", "/cost-estimate"),
    ("arxiv.arxiv_llm_bulk", "POST", "/test-small-batch"),
]
_SCOPED_ENDPOINTS = [
    ("connectors.router", "GET", "/"),
    ("connectors.router", "POST", "/search"),
    ("connectors.router", "GET", "/mock_source/record-1"),
    *_GLOBAL_ENDPOINTS,
    ("research_engine.blueprints", "GET", "/templates"),
    ("research_engine.blueprints", "GET", "/templates/auth_test"),
    ("research_engine.capabilities", "GET", ""),
]


@dataclass
class IsolatedAPI:
    client: TestClient
    module: ModuleType
    method: str
    path: str
    body: dict[str, Any] | None
    user: User
    session: Session
    db: AsyncMock
    effects: list[Mock]
    expected_effect: Mock | None

    def request(self, *, authenticated: bool = True) -> Response:
        headers = {"Authorization": "Bearer test-token"} if authenticated else {}
        response = self.client.request(
            self.method, self.path, json=self.body, headers=headers
        )
        if self.path.endswith("/start") and response.status_code == 200:
            # Scheduling is asynchronous; wait on the mock task's own loop before
            # asserting its await receipt, rather than racing the HTTP response.
            assert self.client.portal is not None
            self.client.portal.call(lambda: self.module._ingestion_task)
        return response


@pytest.fixture
def isolated_api(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    mock_db_session: Session,
) -> Iterator[IsolatedAPI]:
    module_name, method, suffix = request.param
    module = import_module(f"src.api.{module_name}")
    app = FastAPI()
    prefix = "" if module_name == "connectors.router" else "/api/v1"
    app.include_router(module.router, prefix=prefix)

    # Profile encryption normally initializes in the full app lifespan.
    # Keep it out of this isolated authorization test; SQL predicates stay real.
    for operation in ("encrypt_sensitive_field", "decrypt_sensitive_field"):
        monkeypatch.setattr(
            f"src.models.encrypted_fields.{operation}",
            lambda value, field_name: value,
        )
    User.__table__.create(mock_db_session.get_bind())
    user = User(
        id=uuid4(),
        email="auth-test@example.com",
        first_name="Auth",
        last_name="Test",
        password_hash="unused",
        role=UserRole.USER,
        organization_id=None,
        is_active=True,
        is_deleted=False,
    )
    mock_db_session.add(user)
    mock_db_session.commit()
    db = AsyncMock()
    db.execute.side_effect = mock_db_session.execute
    db.commit.side_effect = mock_db_session.commit
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(
        "src.core.security.verify_token",
        Mock(
            return_value=TokenData(
                user_id=str(user.id),
                email=user.email,
                exp=datetime.utcnow() + timedelta(hours=1),
            )
        ),
    )
    monkeypatch.setattr(
        settings,
        "PLATFORM_OPERATOR_USER_IDS",
        str(user.id) if module_name.startswith("arxiv.") else str(uuid4()),
    )

    body = {} if method == "POST" else None
    expected_effect = None
    if module_name == "research_engine.blueprints":
        loader = Mock()
        loader.list_templates.return_value = [
            {"slug": "auth_test", "name": "Auth Test", "step_count": 1}
        ]
        loader.load_template.return_value = {
            "name": "Auth Test",
            "steps": [{"type": "search", "name": "Search"}],
            "parameters": {},
        }
        factory = Mock(return_value=loader)
        monkeypatch.setattr(module, "BlueprintLoader", factory)
        effects = [factory, loader]
        expected_effect = (
            loader.list_templates if suffix == "/templates" else loader.load_template
        )
    elif module_name == "research_engine.capabilities":
        projection = Mock(wraps=module.safe_capability_projection)
        monkeypatch.setattr(module, "safe_capability_projection", projection)
        effects = [projection]
        expected_effect = projection
    elif module_name == "connectors.router":
        record = ConnectorResult(
            id="record-1", title="External record", source="mock_source", url=""
        )
        connector = Mock()
        connector.info = ConnectorInfo(
            name="mock_source",
            display_name="Mock Source",
            description="External catalog",
            domains=[ConnectorDomain.GENERAL],
            capabilities=[ConnectorCapability.SEARCH, ConnectorCapability.FETCH],
            base_url="",
        )
        connector.is_available.return_value = True
        connector.search = AsyncMock(return_value=[record])
        connector.fetch_by_id = AsyncMock(return_value=record)
        registry = Mock()
        registry.list_available.return_value = [connector]
        registry.get.return_value = connector
        monkeypatch.setattr(module, "connector_registry", registry)
        effects = [registry, connector]
        if suffix == "/":
            expected_effect = registry.list_available
        elif suffix == "/search":
            body = {"query": "auth test", "connectors": ["mock_source"]}
            expected_effect = connector.search
        else:
            expected_effect = connector.fetch_by_id
    else:
        llm = module_name.endswith("arxiv_llm_bulk")
        service = Mock()
        service.run_ingestion = AsyncMock(return_value={"total_ingested": 1})
        factory = Mock(return_value=service)
        task = AsyncMock()
        monkeypatch.setattr(
            module,
            "KaggleLLMBulkIngestionService" if llm else "KaggleBulkIngestionService",
            factory,
        )
        monkeypatch.setattr(
            module, "_run_llm_ingestion_task" if llm else "_run_ingestion_task", task
        )
        running = Mock()
        running.done.return_value = False
        monkeypatch.setattr(
            module,
            "_ingestion_task",
            running if suffix in {"/status", "/stop"} else None,
        )
        monkeypatch.setattr(module, "_current_ingestion", {"total_ingested": 1})
        neo4j_auth = Mock(
            return_value=SimpleNamespace(
                uri="bolt://example.invalid", user="test", password="unused"
            )
        )
        monkeypatch.setattr(module, "get_neo4j_auth", neo4j_auth)
        graph_result = MagicMock()
        graph_result.single = AsyncMock(return_value={"count": 0})
        graph_result.__aiter__.return_value = []
        graph_session = AsyncMock()
        graph_session.run.return_value = graph_result
        driver = AsyncMock()
        driver.session = Mock(return_value=graph_session)
        graph_session.__aenter__.return_value = graph_session
        driver_factory = Mock(return_value=driver)
        monkeypatch.setattr("neo4j.AsyncGraphDatabase.driver", driver_factory)
        effects = [factory, service, task, running, neo4j_auth, driver_factory]
        expected_effect = {
            "/start": task,
            "/status": running.done,
            "/stop": running.cancel,
            "/stats": driver_factory,
            "/test-small-batch": service.run_ingestion,
        }.get(suffix)

    with TestClient(app) as client:
        yield IsolatedAPI(
            client,
            module,
            method,
            prefix + module.router.prefix + suffix,
            body,
            user,
            mock_db_session,
            db,
            effects,
            expected_effect,
        )


@pytest.mark.parametrize(
    "isolated_api", _SCOPED_ENDPOINTS, indirect=True, ids=lambda case: ":".join(case)
)
def test_scoped_router_denies_anonymous_before_effects(
    isolated_api: IsolatedAPI,
) -> None:
    response = isolated_api.request(authenticated=False)

    assert response.status_code == 401, response.text
    isolated_api.db.execute.assert_not_awaited()
    for effect in isolated_api.effects:
        assert effect.mock_calls == []


@pytest.mark.parametrize(
    "isolated_api", _SCOPED_ENDPOINTS, indirect=True, ids=lambda case: ":".join(case)
)
def test_scoped_router_denies_inactive_user_before_effects(
    isolated_api: IsolatedAPI,
) -> None:
    isolated_api.user.is_active = False
    isolated_api.session.commit()

    response = isolated_api.request()

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "User not found or inactive"
    assert isolated_api.db.execute.await_count > 0
    for effect in isolated_api.effects:
        assert effect.mock_calls == []


@pytest.mark.parametrize(
    "isolated_api", _GLOBAL_ENDPOINTS, indirect=True, ids=lambda case: ":".join(case)
)
def test_global_router_denies_tenant_admin_before_effects(
    isolated_api: IsolatedAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_api.user.role = UserRole.ADMIN
    isolated_api.session.commit()
    monkeypatch.setattr(settings, "PLATFORM_OPERATOR_USER_IDS", str(uuid4()))

    response = isolated_api.request()

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Platform operator access required"
    for effect in isolated_api.effects:
        assert effect.mock_calls == []


@pytest.mark.parametrize(
    "isolated_api", _SCOPED_ENDPOINTS, indirect=True, ids=lambda case: ":".join(case)
)
def test_scoped_router_allows_authorized_user_with_mocked_effects(
    isolated_api: IsolatedAPI,
) -> None:
    response = isolated_api.request()

    assert response.status_code == 200, response.text
    isolated_api.db.execute.assert_awaited_once()
    if isinstance(isolated_api.expected_effect, AsyncMock):
        isolated_api.expected_effect.assert_awaited_once()
    elif isolated_api.expected_effect is not None:
        isolated_api.expected_effect.assert_called_once()
