"""Every remaining search / KG / search-quality route must 403 an org-less
caller before touching the database or a service.

``User.organization_id`` is nullable and ``str(None)`` is ``"None"``; these
handlers used to pass that (or ``None``) on as a tenant scope or stamp it on
new graph nodes.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.search import knowledge_graph as kg_api
from src.api.search import search as search_api
from src.api.search import search_quality as quality_api

pytestmark = pytest.mark.unit

ROUTES: list[tuple[Any, str]] = [
    (kg_api, "create_entity"),
    (kg_api, "create_relationship"),
    (kg_api, "batch_create_entities"),
    (kg_api, "create_extraction_job"),
    (kg_api, "extract_entities_from_document"),
    (search_api, "get_search_suggestions"),
    (search_api, "get_search_analytics"),
    (search_api, "get_search_indexes"),
    (search_api, "reindex_document"),
    (quality_api, "evaluate_search_quality"),
    (quality_api, "submit_user_feedback"),
    (quality_api, "get_quality_analytics"),
]


class _Tripwire:
    """Fails the test if the handler touches it at all (attribute, call,
    iteration, truthiness) before the org check."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def _fail(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"{self._name} used before the org check")

    def __getattr__(self, _attr: str) -> Any:
        self._fail()

    __call__ = _fail
    __iter__ = _fail
    __bool__ = _fail
    __len__ = _fail


def _tripwire(name: str) -> Any:
    return _Tripwire(name)


def _kwargs(fn: Callable[..., Any], user: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for name in inspect.signature(fn).parameters:
        kwargs[name] = user if name == "current_user" else _tripwire(name)
    return kwargs


@pytest.mark.parametrize("org_id", [None, ""])
@pytest.mark.parametrize(
    "module,name",
    ROUTES,
    ids=[f"{m.__name__.rsplit('.', 1)[-1]}.{n}" for m, n in ROUTES],
)
async def test_route_rejects_org_less_caller(
    monkeypatch: pytest.MonkeyPatch, module: Any, name: str, org_id: str | None
) -> None:
    for service in (
        "knowledge_graph_service",
        "fulltext_search_service",
        "search_quality_service",
    ):
        if hasattr(module, service):
            monkeypatch.setattr(module, service, _tripwire(service))

    fn = getattr(module, name)
    user = SimpleNamespace(id="user-1", organization_id=org_id, role=MagicMock())
    with pytest.raises(HTTPException) as exc:
        result = fn(**_kwargs(fn, user))
        if inspect.isawaitable(result):
            await result
    assert exc.value.status_code == 403
