"""GOO-399: two-account search and suggestion checks on PostgreSQL.

Matrix: docs/engineering/data-isolation-matrix.md (row S2). SQLite cannot run
the full-text paths, so these tests use the ``pg_*`` fixtures from
``conftest.py`` and skip unless ``TWO_ACCOUNT_PG_TEST_DATABASE_URL`` names a
disposable database (CI sets it and rejects a skipped run).

Routes, all mounted by ``src.main``:

- Organization-scoped documents: ``POST /api/v1/search/`` (fulltext, hybrid,
  semantic, knowledge_graph), ``POST /api/v1/search/hybrid``,
  ``GET /api/v1/search/suggestions`` and the org query log
  ``GET /api/v1/search/analytics``.
- Membership-scoped chat (owner, member or ``is_public`` workspace):
  ``POST|GET /api/v2/search/threads``, ``POST|GET /api/v2/search/messages``,
  ``GET /api/v2/search/combined`` and ``GET /api/v2/search/suggestions``.

Every request uses a real token through ``MultiTenancyMiddleware`` and the
real ``get_current_user``. Only external services are stubbed, never an
access predicate: the Neo4j entity search returns nothing and records the
scope it was given, and Cohere reranking is off.

A leak check looks for each canary, its 8-hex tag (which survives the
lower-cased, punctuation-stripped suggestion form and ``ts_headline``
highlighting) and the row id.

Mutation-verified (2026-10-08, PostgreSQL 14): each guard below was disabled,
the named tests failed naming the leaked canaries, and ``git diff backend/src``
was empty after restoring it. Focused command:
``TWO_ACCOUNT_PG_TEST_DATABASE_URL=... pytest <this file> -c backend/pytest.ini -k <name>``.

- ``fulltext_search_service._build_search_query``
  ``AND d.organization_id = :organization_id``:
  ``test_document_search_excludes_foreign_org``, ``..._pages_never_cross_org``.
- ``_build_count_query`` organization predicate: the fulltext case of
  ``test_document_search_excludes_foreign_org`` (total 5, expected 2).
- ``_get_search_suggestions`` organization predicate: ``-k suggestions``.
- ``_build_search_query`` ``d.is_deleted = false``:
  ``test_soft_deleted_document_is_absent``.
- ``thread_message_search_service._WORKSPACE_ACCESS_PREDICATE``:
  ``-k "chat_search or message_author"``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.models import Conversation, Document, Thread, Workspace, WorkspaceMember
from src.models.search_schemas import SearchQuery, SearchType
from tests.integration.two_account.conftest import (
    Clients,
    PgEngines,
    add_member,
    canary,
    sid,
    soft_delete,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

QUERY = "ISO-CANARY"  # matches every seeded title, content and message
ORG_A, ORG_B = str(sid("org-a")), str(sid("org-b"))
A_DOCS = ("a-doc", "a-doc-2")
ORG_B_DOCS = ("b-doc", "b-pub-doc", "a-old-org-doc")


def _text(body: bytes | str) -> str:
    return body.decode("utf-8", "replace") if isinstance(body, bytes) else body


def _leaked(body: bytes | str, keys: Iterable[str], ids: Iterable[str]) -> List[str]:
    text = _text(body)
    found = [k for k in keys if canary(k) in text or sid(k).hex[:8] in text]
    return found + [f"id:{k}" for k in ids if str(sid(k)) in text]


def assert_no_documents(body: bytes | str, *docs: str) -> None:
    keys = [f"{d}-{part}" for d in docs for part in ("title", "content")]
    leaked = _leaked(body, keys, docs)
    assert not leaked, f"leaked documents: {leaked}"


def assert_no_chat(body: bytes | str, *chains: str) -> None:
    keys = [f"{c}-{part}" for c in chains for part in ("ws", "conv", "thread", "msg")]
    ids = [f"{c}-{part}" for c in chains for part in ("conv", "thread", "msg")]
    leaked = _leaked(body, keys, ids)
    assert not leaked, f"leaked chat rows: {leaked}"


def _answer(r: Response) -> str:
    """The response minus ``filters_applied``, which only echoes the caller's
    own request (a filter naming B's ids is A's input, not B's data)."""
    body = r.json()
    body.pop("filters_applied", None)
    return json.dumps(body)


def _ids(rows: List[Dict[str, Any]], field: str) -> set:
    return {row[field] for row in rows}


def _doc_ids(*docs: str) -> set:
    return {str(sid(d)) for d in docs}


@pytest.fixture(autouse=True)
def graph_calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Stub the external services the search pipeline can reach.

    Neo4j: ``search_entities`` returns no entities and records the scope it
    received. Cohere: reranking is disabled. Neither stub touches the
    PostgreSQL organization or membership predicates under test.
    """
    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )
    from src.services.search.cohere_rerank_service import cohere_rerank_service

    calls: List[Dict[str, Any]] = []

    def search_entities(
        query: str,
        entity_types: Any = None,
        limit: int = 50,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> list:
        calls.append(
            {
                "organization_id": organization_id,
                "source_document_ids": source_document_ids,
            }
        )
        return []

    monkeypatch.setattr(knowledge_graph_service, "search_entities", search_entities)
    monkeypatch.setattr(cohere_rerank_service, "_enabled", False)
    return calls


# --- v1 document search: organization-scoped -------------------------------

# (route, search_type, query). "who ..." also routes the hybrid pipeline
# through its knowledge-graph arm; the stopword drops out of the tsquery.
DOC_SEARCHES = [
    ("/api/v1/search/", "fulltext", QUERY),
    ("/api/v1/search/", "hybrid", f"who {QUERY}"),
    ("/api/v1/search/", "semantic", QUERY),
    ("/api/v1/search/hybrid", "hybrid", f"who {QUERY}"),
]


async def _search(
    client: AsyncClient, route: str, search_type: str, query: str, **body: Any
) -> Response:
    payload: Dict[str, Any] = {"query": query, "search_type": search_type}
    payload.update(body)
    return await client.post(route, json=payload)


@pytest.mark.parametrize("route,search_type,query", DOC_SEARCHES)
async def test_document_search_excludes_foreign_org(
    pg_clients: Clients,
    graph_calls: List[Dict[str, Any]],
    route: str,
    search_type: str,
    query: str,
) -> None:
    """A gets exactly its two documents: no org-b title, snippet, id,
    suggestion or org-b-inflated total, including ``a-old-org-doc`` that A
    uploaded while it belonged to org-b."""
    r = await _search(pg_clients("A"), route, search_type, query, limit=20)
    assert r.status_code == 200, r.text
    assert_no_documents(r.content, *ORG_B_DOCS)
    body = r.json()
    assert _ids(body["results"], "document_id") == _doc_ids(*A_DOCS)
    assert body["total_results"] == 2
    if query.startswith("who "):
        assert graph_calls, "the hybrid knowledge-graph arm did not run"
    assert all(call["organization_id"] == ORG_A for call in graph_calls)


@pytest.mark.parametrize("route,search_type,query", DOC_SEARCHES)
async def test_document_search_positive_control_same_org(
    pg_clients: Clients, route: str, search_type: str, query: str
) -> None:
    """C (org-b) gets every org-b document for the same query, so the
    negative case above is a real denial, not an empty index."""
    r = await _search(pg_clients("C"), route, search_type, query, limit=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert _ids(body["results"], "document_id") == _doc_ids(*ORG_B_DOCS)
    assert body["total_results"] == 3
    assert canary("b-doc-title") in r.text
    assert_no_documents(r.content, *A_DOCS)


async def test_fulltext_suggestions_in_search_response_are_org_scoped(
    pg_clients: Clients,
) -> None:
    """Fewer than five hits returns title suggestions; they stay in-org."""
    a = await _search(pg_clients("A"), "/api/v1/search/", "fulltext", QUERY)
    c = await _search(pg_clients("C"), "/api/v1/search/", "fulltext", QUERY)
    a_suggestions = " ".join(a.json()["suggestions"] or [])
    c_suggestions = " ".join(c.json()["suggestions"] or [])
    assert sid("a-doc-title").hex[:8] in a_suggestions
    assert_no_documents(a_suggestions, *ORG_B_DOCS)
    assert sid("b-doc-title").hex[:8] in c_suggestions
    assert_no_documents(c_suggestions, *A_DOCS)


@pytest.mark.parametrize("route,search_type,query", DOC_SEARCHES)
async def test_document_search_pages_never_cross_org(
    pg_clients: Clients, route: str, search_type: str, query: str
) -> None:
    """Walking one-result pages past the end never yields an org-b row and
    every page reports A's own total."""
    seen: set = set()
    for offset in range(4):
        r = await _search(
            pg_clients("A"),
            route,
            search_type,
            query,
            limit=1,
            offset=offset,
            sort_order="title_asc",
        )
        assert r.status_code == 200, r.text
        assert_no_documents(r.content, *ORG_B_DOCS)
        body = r.json()
        assert body["total_results"] == 2, offset
        assert len(body["results"]) == (1 if offset < 2 else 0), offset
        seen |= _ids(body["results"], "document_id")
    assert seen == _doc_ids(*A_DOCS)


# (filters, documents A must get back). Caller-supplied filters narrow A's
# own organization; none of them can name another organization's rows.
DOC_FILTERS = [
    ({"document_ids": [str(sid(d)) for d in ORG_B_DOCS]}, ()),
    ({"organization_id": ORG_B}, A_DOCS),
    ({"is_public": True}, ()),
    ({"uploaded_by_user_id": str(sid("user-b"))}, ()),
    ({"uploaded_by_user_id": str(sid("user-a"))}, A_DOCS),
]


@pytest.mark.parametrize("filters,expected", DOC_FILTERS)
@pytest.mark.parametrize("route,search_type,query", DOC_SEARCHES[:2])
async def test_document_search_filters_cannot_reach_foreign_org(
    pg_clients: Clients,
    route: str,
    search_type: str,
    query: str,
    filters: Dict[str, Any],
    expected: tuple,
) -> None:
    r = await _search(
        pg_clients("A"), route, search_type, query, limit=20, filters=filters
    )
    assert r.status_code == 200, r.text
    assert_no_documents(_answer(r), *ORG_B_DOCS)
    body = r.json()
    assert _ids(body["results"], "document_id") == _doc_ids(*expected)
    assert body["total_results"] == len(expected)


async def test_knowledge_graph_search_scopes_neo4j_to_caller_org(
    pg_clients: Clients, graph_calls: List[Dict[str, Any]]
) -> None:
    """The knowledge-graph search type derives its Neo4j scope from the
    PostgreSQL documents table: only A's organization and A's documents."""
    r = await _search(pg_clients("A"), "/api/v1/search/", "knowledge_graph", QUERY)
    assert r.status_code == 200, r.text
    assert_no_documents(r.content, *ORG_B_DOCS)
    assert graph_calls, "the knowledge-graph search type never reached Neo4j"
    for call in graph_calls:
        assert call["organization_id"] == ORG_A
        assert set(call["source_document_ids"]) == _doc_ids(*A_DOCS)


async def test_vector_search_type_is_rejected(pg_clients: Clients) -> None:
    r = await _search(pg_clients("A"), "/api/v1/search/", "vector", QUERY)
    assert r.status_code == 400
    assert_no_documents(r.content, *ORG_B_DOCS)


# --- v1 suggestions and analytics ------------------------------------------


@pytest.mark.parametrize("q", [QUERY, canary("b-doc-title"), "b-doc-title"])
async def test_title_suggestions_exclude_foreign_org(
    pg_clients: Clients, q: str
) -> None:
    """Including a query that is B's exact private title."""
    r = await pg_clients("A").get(
        "/api/v1/search/suggestions", params={"q": q, "limit": 20}
    )
    assert r.status_code == 200, r.text
    assert_no_documents(r.content, *ORG_B_DOCS)


async def test_title_suggestions_positive_control(pg_clients: Clients) -> None:
    a = await pg_clients("A").get(
        "/api/v1/search/suggestions", params={"q": QUERY, "limit": 20}
    )
    # Suggestions are lower-cased titles with punctuation turned into spaces.
    assert {s["text"] for s in a.json()["suggestions"]} == {
        f"iso canary {d.replace('-', ' ')} title {sid(f'{d}-title').hex[:8]}"
        for d in A_DOCS
    }
    c = await pg_clients("C").get(
        "/api/v1/search/suggestions",
        params={"q": canary("b-doc-title"), "limit": 20},
    )
    assert c.status_code == 200, c.text
    assert sid("b-doc-title").hex[:8] in c.text


async def test_search_analytics_keep_queries_inside_org(pg_clients: Clients) -> None:
    """The analytics query log is per organization: B's private query text
    reaches B's colleague C, never A."""
    b_query = canary("b-query")
    r = await _search(pg_clients("B"), "/api/v1/search/", "fulltext", b_query)
    assert r.status_code == 200, r.text
    a = await pg_clients("A").get("/api/v1/search/analytics")
    assert a.status_code == 200, a.text
    assert b_query not in a.text
    assert a.json()["total_searches"] == 0
    c = await pg_clients("C").get("/api/v1/search/analytics")
    assert c.status_code == 200, c.text
    assert b_query in c.text
    assert c.json()["total_searches"] == 1


# --- soft-deleted documents ------------------------------------------------


async def test_soft_deleted_document_is_absent(
    pg_clients: Clients, pg_db: AsyncSession
) -> None:
    """After B's private document is soft-deleted, its colleague C no longer
    finds it in any result, total or suggestion."""
    await soft_delete(pg_db, Document, "b-doc")
    c = pg_clients("C")
    for route, search_type, query in DOC_SEARCHES:
        r = await _search(c, route, search_type, query, limit=20)
        assert r.status_code == 200, (route, search_type, r.text)
        assert_no_documents(r.content, "b-doc")
        body = r.json()
        assert _ids(body["results"], "document_id") == _doc_ids(
            "b-pub-doc", "a-old-org-doc"
        )
        assert body["total_results"] == 2
    for q in (QUERY, canary("b-doc-title")):
        r = await c.get("/api/v1/search/suggestions", params={"q": q, "limit": 20})
        assert r.status_code == 200, r.text
        assert_no_documents(r.content, "b-doc")


# --- v2 thread and message search: membership-scoped -----------------------

# What each caller may see: own chains plus the public ``b-pub`` workspace.
# Organization co-location grants nothing, so C (org-b) does not see ``b``.
VISIBLE_CHAINS = {"A": ("a", "b-pub"), "B": ("b", "b-pub"), "C": ("b-pub",)}
HIDDEN_CHAINS = {"A": ("b",), "B": ("a",), "C": ("a", "b")}


def chat_searches(**params: Any) -> list:
    """(method, url, kwargs, id field, kind) for each v2 search route that
    accepts every given filter (thread search has no ``thread_id`` filter)."""
    query = {"query": QUERY, **params}
    filters = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    body = {
        "query": QUERY,
        "filters": filters or None,
        **{k: v for k, v in params.items() if k in ("limit", "offset")},
    }
    routes = [
        ("post", "/api/v2/search/messages", {"json": body}, "message_id", "msg"),
        ("get", "/api/v2/search/messages", {"params": query}, "message_id", "msg"),
    ]
    if "thread_id" not in filters:
        routes += [
            ("post", "/api/v2/search/threads", {"json": body}, "thread_id", "thread"),
            ("get", "/api/v2/search/threads", {"params": query}, "thread_id", "thread"),
        ]
    return routes


async def _chat_search(
    client: AsyncClient, method: str, url: str, kwargs: dict
) -> Response:
    response: Response = await getattr(client, method)(url, **kwargs)
    return response


@pytest.mark.parametrize("who", ["A", "C"])
async def test_chat_search_excludes_private_workspace(
    pg_clients: Clients, who: str
) -> None:
    """A (other org) and C (same org, not a member) never get a title,
    snippet, id or count from B's private workspace."""
    client = pg_clients(who)
    visible = VISIBLE_CHAINS[who]
    for method, url, kwargs, field, kind in chat_searches():
        r = await _chat_search(client, method, url, kwargs)
        assert r.status_code == 200, (method, url, r.text)
        assert_no_chat(r.content, *HIDDEN_CHAINS[who])
        body = r.json()
        assert _ids(body["results"], field) == {
            str(sid(f"{c}-{kind}")) for c in visible
        }, (method, url)
        assert body["total_results"] == len(visible), (method, url)
    r = await client.get("/api/v2/search/combined", params={"query": QUERY})
    assert r.status_code == 200, r.text
    assert_no_chat(r.content, *HIDDEN_CHAINS[who])
    assert r.json()["total_results"] == 2 * len(visible)


async def test_chat_search_positive_control_owner_and_member(
    pg_clients: Clients, pg_db: AsyncSession
) -> None:
    """B finds its private workspace; C finds it once invited, and loses it
    again when the membership is removed."""
    for method, url, kwargs, field, kind in chat_searches():
        r = await _chat_search(pg_clients("B"), method, url, kwargs)
        assert str(sid(f"b-{kind}")) in _ids(r.json()["results"], field), url
    await add_member(pg_db, "b", "C")
    for method, url, kwargs, field, kind in chat_searches():
        r = await _chat_search(pg_clients("C"), method, url, kwargs)
        assert r.json()["total_results"] == 2, url
        assert canary(f"b-{kind}") in r.text, url
    await soft_delete(pg_db, WorkspaceMember, "member-b-C")
    for method, url, kwargs, _field, _kind in chat_searches():
        r = await _chat_search(pg_clients("C"), method, url, kwargs)
        assert_no_chat(r.content, "b")
        assert r.json()["total_results"] == 1, url


async def test_chat_search_pages_never_cross_membership(pg_clients: Clients) -> None:
    for page in range(4):
        for method, url, kwargs, field, _kind in chat_searches(limit=1, offset=page):
            r = await _chat_search(pg_clients("A"), method, url, kwargs)
            assert r.status_code == 200, (url, page, r.text)
            assert_no_chat(r.content, "b")
            body = r.json()
            assert body["total_results"] == 2, (url, page)
            assert len(body["results"]) == (1 if page < 2 else 0), (url, page)


@pytest.mark.parametrize(
    "filters",
    [
        {"workspace_id": str(sid("b-ws"))},
        {"conversation_id": str(sid("b-conv"))},
        {"thread_id": str(sid("b-thread"))},
    ],
)
async def test_chat_search_filters_cannot_name_private_workspace(
    pg_clients: Clients, filters: Dict[str, str]
) -> None:
    """Naming B's private workspace, conversation or thread returns nothing."""
    for method, url, kwargs, _field, _kind in chat_searches(**filters):
        r = await _chat_search(pg_clients("A"), method, url, kwargs)
        assert r.status_code == 200, (method, url, r.text)
        assert_no_chat(_answer(r), "b")
        assert r.json()["total_results"] == 0, (method, url)
    combined = {k: v for k, v in filters.items() if k != "thread_id"}
    if combined:
        r = await pg_clients("A").get(
            "/api/v2/search/combined", params={"query": QUERY, **combined}
        )
        assert r.status_code == 200, r.text
        assert_no_chat(_answer(r), "b")
        assert r.json()["total_results"] == 0


async def test_message_author_filter_returns_only_public_rows(
    pg_clients: Clients,
) -> None:
    """Filtering by B as author yields B's public message, never the private one."""
    r = await pg_clients("A").get(
        "/api/v2/search/messages",
        params={"query": QUERY, "user_id": str(sid("user-b"))},
    )
    assert r.status_code == 200, r.text
    assert_no_chat(r.content, "b")
    assert _ids(r.json()["results"], "message_id") == {str(sid("b-pub-msg"))}


@pytest.mark.parametrize(
    "model,key", [(Thread, "b-thread"), (Conversation, "b-conv"), (Workspace, "b-ws")]
)
async def test_chat_search_drops_soft_deleted_ancestors(
    pg_clients: Clients, pg_db: AsyncSession, model: Any, key: str
) -> None:
    """Soft-deleting the thread or any ancestor hides it even from the owner."""
    await soft_delete(pg_db, model, key)
    for method, url, kwargs, _field, _kind in chat_searches():
        r = await _chat_search(pg_clients("B"), method, url, kwargs)
        assert r.status_code == 200, (method, url, r.text)
        assert_no_chat(r.content, "b")
        assert r.json()["total_results"] == 1, (method, url)
    r = await pg_clients("B").get("/api/v2/search/combined", params={"query": QUERY})
    assert_no_chat(r.content, "b")


# --- v2 thread-title suggestions -------------------------------------------


@pytest.mark.parametrize("who", ["A", "C"])
async def test_thread_suggestions_exclude_private_workspace(
    pg_clients: Clients, who: str
) -> None:
    """No caller outside workspace ``b`` gets its thread title. The route
    currently answers 500 on PostgreSQL (see the xfail control below), so
    this only proves the error body leaks nothing until that is fixed."""
    for q in (QUERY, canary("b-thread")):
        r = await pg_clients(who).get(
            "/api/v2/search/suggestions", params={"query": q, "limit": 10}
        )
        assert r.status_code in (200, 500), r.text
        assert_no_chat(r.content, *HIDDEN_CHAINS[who])


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "GOO-399 finding, not a leak: GET /api/v2/search/suggestions runs "
        "SELECT DISTINCT t.title ... ORDER BY t.last_message_at, which "
        "PostgreSQL rejects, so the route answers 500 for every caller"
    ),
)
async def test_thread_suggestions_positive_control(pg_clients: Clients) -> None:
    r = await pg_clients("B").get(
        "/api/v2/search/suggestions", params={"query": QUERY, "limit": 10}
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["suggestions"]) == {
        canary("b-thread"),
        canary("b-pub-thread"),
    }


# --- missing organization: fail closed -------------------------------------


def _query(text: str, search_type: SearchType) -> SearchQuery:
    return SearchQuery.model_validate({"query": text, "search_type": search_type})


ALL_SEARCH_REQUESTS = [
    ("post", "/api/v1/search/", {"json": {"query": QUERY}}),
    ("post", "/api/v1/search/", {"json": {"query": QUERY, "search_type": "hybrid"}}),
    ("post", "/api/v1/search/hybrid", {"json": {"query": QUERY}}),
    ("get", "/api/v1/search/suggestions", {"params": {"q": QUERY}}),
    ("get", "/api/v1/search/analytics", {}),
    ("post", "/api/v2/search/threads", {"json": {"query": QUERY}}),
    ("get", "/api/v2/search/threads", {"params": {"query": QUERY}}),
    ("post", "/api/v2/search/messages", {"json": {"query": QUERY}}),
    ("get", "/api/v2/search/messages", {"params": {"query": QUERY}}),
    ("get", "/api/v2/search/combined", {"params": {"query": QUERY}}),
    ("get", "/api/v2/search/suggestions", {"params": {"query": QUERY}}),
]
EVERY_DOCUMENT = A_DOCS + ORG_B_DOCS


@pytest.mark.parametrize("method,url,kwargs", ALL_SEARCH_REQUESTS)
async def test_missing_organization_caller_is_rejected(
    pg_clients: Clients, method: str, url: str, kwargs: dict
) -> None:
    """D is active but belongs to no organization: the tenancy gate answers
    401 before any search runs (even ``b-pub`` stays out of reach)."""
    r = await getattr(pg_clients("D"), method)(url, **kwargs)
    assert r.status_code == 401, r.text
    assert_no_documents(r.content, *EVERY_DOCUMENT)
    assert_no_chat(r.content, "a", "b", "b-pub")


@pytest.mark.parametrize("organization_id", [None, "", "None", "not-a-uuid"])
async def test_document_search_services_fail_closed_without_organization(
    pg_seed: None, pg_engines: PgEngines, organization_id: Optional[str]
) -> None:
    """Below the HTTP gate, the full-text builder, its suggestions and the
    hybrid full-text arm return nothing without a valid organization scope
    (GOO-351), against real seeded rows."""
    from src.services.search.fulltext_search_service import fulltext_search_service
    from src.services.search.hybrid_search_service import hybrid_search_service

    user_id = str(sid("user-d"))
    with Session(pg_engines.sync) as db:
        responses = [
            fulltext_search_service.search(
                search_request=_query(QUERY, SearchType.FULLTEXT),
                user_id=user_id,
                organization_id=organization_id,
                db=db,
            ),
            hybrid_search_service.search(
                search_request=_query(QUERY, SearchType.HYBRID),
                user_id=user_id,
                organization_id=organization_id,
                db=db,
            ),
        ]
        for response in responses:
            assert response.results == [], response.search_type
            assert response.total_results == 0, response.search_type
            assert_no_documents(response.model_dump_json(), *EVERY_DOCUMENT)
        assert (
            fulltext_search_service._get_search_suggestions(QUERY, db, organization_id)
            == []
        )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "GOO-399 latent finding: HybridSearchService forwards a missing "
        "organization_id to the Neo4j arm, whose scope predicate is then "
        "empty (cross-tenant). Unreachable through the S2 routes: the "
        "tenancy gate rejects org-less callers with 401 and the routes pass "
        "str(organization_id). The agent's _legacy_hybrid_search_fallback "
        "passes None when its organization id is empty."
    ),
)
async def test_hybrid_search_without_organization_never_queries_graph_unscoped(
    pg_seed: None, pg_engines: PgEngines, graph_calls: List[Dict[str, Any]]
) -> None:
    from src.services.search.hybrid_search_service import hybrid_search_service

    with Session(pg_engines.sync) as db:
        hybrid_search_service.search(
            search_request=_query(f"who {QUERY}", SearchType.HYBRID),
            user_id=str(sid("user-d")),
            organization_id=None,
            db=db,
        )
    unscoped = [
        call
        for call in graph_calls
        if call["organization_id"] is None and call["source_document_ids"] is None
    ]
    assert not unscoped, f"unscoped knowledge-graph queries: {len(unscoped)}"
