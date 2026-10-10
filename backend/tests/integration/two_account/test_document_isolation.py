"""GOO-352: two-account document, file, citation and search checks.

Matrix: docs/engineering/data-isolation-matrix.md (rows D*, F*, CI*, S*).
Every request goes through an authenticated HTTP client; nothing mocks a
guard. Rows marked ``xfail(strict=True)`` are confirmed leaks owned by the
named fix issue — they flip to XPASS (and fail) once the fix lands, at which
point delete the marker.
"""

import re
from pathlib import Path
from typing import Any, Callable

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Collection, CollectionDocument, Document
from tests.integration.two_account.conftest import (
    Clients,
    _citation,
    _document,
    add_member,
    assert_no_canary,
    canary,
    sid,
    soft_delete,
)

pytestmark = pytest.mark.integration


DOC_READ_ROUTES = (
    "/api/v1/files/{id}",
    "/api/v1/files/{id}/download",
    "/api/v1/files/{id}/content",
    "/api/v1/files/{id}/metadata",
    "/api/v1/documents/{id}",
    "/api/v1/documents/{id}/status",
)


def _doc_keys(doc: str) -> tuple:
    return (f"{doc}-title", f"{doc}-content")


# --- D1/F1: foreign-organization documents are invisible by id ------------


@pytest.mark.parametrize("route", DOC_READ_ROUTES)
@pytest.mark.parametrize("doc", ["b-doc", "b-pub-doc", "a-old-org-doc"])
async def test_foreign_org_document_reads_are_404(
    clients: Clients, route: str, doc: str
) -> None:
    """Org-b documents (private, public, or once uploaded by A) are 404 to A."""
    r = await clients("A").get(route.format(id=sid(doc)))
    assert r.status_code == 404
    assert_no_canary(r.content, *_doc_keys(doc))


@pytest.mark.parametrize("route", DOC_READ_ROUTES)
async def test_own_document_reads_succeed(clients: Clients, route: str) -> None:
    r = await clients("A").get(route.format(id=sid("a-doc")))
    assert r.status_code == 200


async def test_own_download_returns_bytes(clients: Clients) -> None:
    r = await clients("A").get(f"/api/v1/files/{sid('a-doc')}/download")
    assert r.status_code == 200
    assert r.text == canary("a-doc-content")


# --- D2: same-organization documents are shared by design -----------------


@pytest.mark.parametrize("route", DOC_READ_ROUTES)
async def test_same_org_colleague_reads_shared_document(
    clients: Clients, route: str
) -> None:
    """C shares org-b with B, so B's private document is readable (org-scoped)."""
    r = await clients("C").get(route.format(id=sid("b-doc")))
    assert r.status_code == 200


# --- D3: deleted documents disappear everywhere ---------------------------


async def test_deleted_document_disappears_for_colleague(
    clients: Clients, test_db: AsyncSession
) -> None:
    await soft_delete(test_db, Document, "b-doc")
    c = clients("C")
    for route in DOC_READ_ROUTES:
        r = await c.get(route.format(id=sid("b-doc")))
        assert r.status_code == 404, route
    for r in (
        await c.get("/api/v1/documents"),
        await c.get("/api/v1/files/?search=ISO-CANARY"),
        await c.post("/api/v1/documents/search?query=ISO-CANARY"),
    ):
        assert r.status_code == 200
        assert_no_canary(r.content, *_doc_keys("b-doc"))


# --- S1: lists, search and their counts stay inside the organization ------

FOREIGN_DOC_KEYS = _doc_keys("b-doc") + _doc_keys("b-pub-doc")


@pytest.mark.parametrize(
    "method,url,total",
    [
        ("get", "/api/v1/documents", lambda j: j["pagination"]["total"]),
        ("get", "/api/v1/documents?page=2&size=1", lambda j: j["pagination"]["total"]),
        ("get", "/api/v1/files/?search=ISO-CANARY", lambda j: j["total"]),
        (
            "post",
            "/api/v1/documents/search?query=ISO-CANARY",
            lambda j: j["pagination"]["total"],
        ),
        (
            "post",
            "/api/v1/documents/search?query=ISO-CANARY&page=2&size=1",
            lambda j: j["pagination"]["total"],
        ),
    ],
)
async def test_lists_and_search_exclude_foreign_org(
    clients: Clients, method: str, url: str, total: Callable[[Any], int]
) -> None:
    """A sees only a-doc: no org-b title/content and no org-b-inflated count."""
    r = await getattr(clients("A"), method)(url)
    assert r.status_code == 200
    assert_no_canary(r.content, *FOREIGN_DOC_KEYS, *_doc_keys("a-old-org-doc"))
    assert total(r.json()) == 1


async def test_search_positive_control_same_org(clients: Clients) -> None:
    """The same query does find org-b rows for C — the negative case is real."""
    r = await clients("C").post("/api/v1/documents/search?query=ISO-CANARY")
    assert r.json()["pagination"]["total"] == 3
    assert canary("b-doc-title") in r.text


# --- CI1: citations -------------------------------------------------------


def _cit_keys(cit: str) -> tuple:
    return (f"{cit}-title", f"{cit}-quote")


def _export(cit: str) -> dict:
    return {"citation_ids": [str(sid(cit))], "format": "bibtex"}


async def test_foreign_private_citation_is_404(clients: Clients) -> None:
    r = await clients("A").get(f"/api/v1/citations/{sid('b-cit')}")
    assert r.status_code == 404
    assert_no_canary(r.content, "b-cit-title", "b-cit-quote")


async def test_own_citation_is_readable(clients: Clients) -> None:
    r = await clients("A").get(f"/api/v1/citations/{sid('a-cit')}")
    assert r.status_code == 200
    assert canary("a-cit-quote") in r.text


@pytest.mark.parametrize("cit", ["b-pub-cit", "a-old-org-cit"])
async def test_foreign_org_citation_detail_is_404(clients: Clients, cit: str) -> None:
    r = await clients("A").get(f"/api/v1/citations/{sid(cit)}")
    assert r.status_code == 404
    assert_no_canary(r.content, f"{cit}-title", f"{cit}-quote")


async def test_citation_list_and_count_exclude_foreign_org(clients: Clients) -> None:
    r = await clients("A").get("/api/v1/citations")
    assert r.status_code == 200
    assert_no_canary(
        r.content, *_cit_keys("b-cit"), *_cit_keys("b-pub-cit"), "a-old-org-cit-title"
    )
    assert r.json()["total"] == 1


@pytest.mark.parametrize("cit", ["b-pub-cit", "a-old-org-cit"])
async def test_citation_export_excludes_foreign_org(clients: Clients, cit: str) -> None:
    r = await clients("A").post(
        "/api/v1/citations/export",
        json={"citation_ids": [str(sid(cit))], "format": "bibtex"},
    )
    assert_no_canary(r.content, f"{cit}-title", f"{cit}-quote")


async def test_citation_export_excludes_foreign_private(clients: Clients) -> None:
    r = await clients("A").post(
        "/api/v1/citations/export",
        json={"citation_ids": [str(sid("b-cit"))], "format": "bibtex"},
    )
    assert_no_canary(r.content, "b-cit-title", "b-cit-quote")


async def test_foreign_private_citation_hidden_from_every_read(
    clients: Clients,
) -> None:
    """CI1 across detail, list + ``total`` and export: org-b's private b-cit
    stays invisible to A now that same-org reads are org-wide (GOO-410)."""
    a = clients("A")
    r = await a.get(f"/api/v1/citations/{sid('b-cit')}")
    assert r.status_code == 404
    assert_no_canary(r.content, *_cit_keys("b-cit"))

    r = await a.get(f"/api/v1/citations?document_id={sid('b-doc')}")
    assert r.status_code == 200
    assert_no_canary(r.content, *_cit_keys("b-cit"))
    assert r.json()["total"] == 0

    r = await a.post("/api/v1/citations/export", json=_export("b-cit"))
    assert r.status_code == 404
    assert_no_canary(r.content, *_cit_keys("b-cit"))


# --- CI4: same-organization citations follow the document (GOO-410) -------

ORG_B_CITATIONS = ("b-cit", "b-pub-cit", "a-old-org-cit")  # on org-b documents


async def test_same_org_colleague_reads_private_citation(clients: Clients) -> None:
    """C shares org-b with B, so b-cit (on B's private b-doc) reads like D2."""
    r = await clients("C").get(f"/api/v1/citations/{sid('b-cit')}")
    assert r.status_code == 200
    assert canary("b-cit-quote") in r.text


async def test_same_org_colleague_citation_list_and_total(clients: Clients) -> None:
    """C lists every org-b document citation, private or public, whoever
    uploaded it; nothing from org-a, and ``total`` counts exactly those."""
    r = await clients("C").get("/api/v1/citations")
    assert r.status_code == 200
    body = r.json()
    assert {c["id"] for c in body["citations"]} == {
        str(sid(c)) for c in ORG_B_CITATIONS
    }
    assert body["total"] == len(ORG_B_CITATIONS)
    assert canary("b-cit-quote") in r.text
    assert_no_canary(r.content, *_cit_keys("a-cit"))


async def test_same_org_colleague_exports_private_citation(clients: Clients) -> None:
    r = await clients("C").post("/api/v1/citations/export", json=_export("b-cit"))
    assert r.status_code == 200
    assert canary("b-cit-title") in r.text


@pytest.mark.parametrize("who", ["B", "C"])
async def test_deleted_document_hides_its_citation(
    clients: Clients, test_db: AsyncSession, who: str
) -> None:
    """Soft-deleting b-doc revokes b-cit for its uploader and the colleague."""
    await soft_delete(test_db, Document, "b-doc")
    client = clients(who)

    r = await client.get(f"/api/v1/citations/{sid('b-cit')}")
    assert r.status_code == 404
    assert_no_canary(r.content, *_cit_keys("b-cit"))

    r = await client.get("/api/v1/citations")
    assert r.status_code == 200
    assert_no_canary(r.content, *_cit_keys("b-cit"))
    assert r.json()["total"] == len(ORG_B_CITATIONS) - 1

    r = await client.post("/api/v1/citations/export", json=_export("b-cit"))
    assert r.status_code == 404
    assert_no_canary(r.content, *_cit_keys("b-cit"))


# --- CI5: project bibliography fallback (GOO-400) ---------------------------
#
# Project ``b-proj`` lives in B's private workspace ``b`` (org-b). C is the
# authorized member. Its document links:
#
#   c-proj-doc  org-b, uploaded by C, no citation  -> the only readable row
#   a-proj-doc  org-a (foreign), ``is_public``, citation ``a-proj-cit``
#   c-gone-doc  org-b, uploaded by C, soft-deleted, citation ``c-gone-cit``
#
# Neither linked citation is readable by C, so the export takes the
# metadata fallback (``Document`` rows, not ``Citation`` rows). Every linked
# document and citation carries DOI/arXiv canaries. Nothing here is "same
# org, uploaded by someone else": GOO-410 changes that rule, and these cases
# must hold under the uploader-or-public rule and the org-shared rule alike.
#
# Mutation check: dropping ``Document.organization_id ==
# current_user.organization_id`` from the fallback query in
# ``export_bibliography`` (backend/src/api/research/citations.py) fails the
# four member cases (a-proj-doc leaks) and the stale-only case (200, not 404).

PROJECT_DOCS = ("c-proj-doc", "a-proj-doc", "c-gone-doc")
HIDDEN_KEYS = tuple(
    f"{row}-{field}"
    for row, fields in (
        ("a-proj-doc", ("title", "content", "doi", "arxiv")),
        ("c-gone-doc", ("title", "content", "doi", "arxiv")),
        ("a-proj-cit", ("title", "quote", "doi", "arxiv")),
        ("c-gone-cit", ("title", "quote", "doi", "arxiv")),
    )
    for field in fields
)
PROJECT_KEYS = HIDDEN_KEYS + tuple(
    f"c-proj-doc-{field}" for field in ("title", "content", "doi", "arxiv")
)


@pytest.fixture
async def bib_project(test_db: AsyncSession, seed: None, tmp_path: Path) -> None:
    """Seed ``b-proj`` with the three links above and invite C into ``b``."""
    docs = [
        _document("c-proj-doc", "c", "b", tmp_path, public=False, identifiers=True),
        _document("a-proj-doc", "a", "a", tmp_path, public=True, identifiers=True),
        _document(
            "c-gone-doc",
            "c",
            "b",
            tmp_path,
            public=False,
            identifiers=True,
            deleted=True,
        ),
    ]
    citations = [
        _citation("a-proj-cit", "a-proj-doc", identifiers=True),
        _citation("c-gone-cit", "c-gone-doc", identifiers=True),
    ]
    project = Collection(
        id=sid("b-proj"), name=canary("b-proj"), workspace_id=sid("b-ws")
    )
    links = [
        CollectionDocument(
            id=sid(f"b-proj-{doc}"),
            collection_id=sid("b-proj"),
            document_id=sid(doc),
        )
        for doc in PROJECT_DOCS
    ]
    for rows in (docs, citations, [project], links):
        test_db.add_all(rows)  # parent before child: SQLite enforces nothing
        await test_db.commit()
    await add_member(test_db, "b", "C")


async def export_project_bibliography(client: AsyncClient, fmt: str) -> Response:
    return await client.post(
        "/api/v1/citations/export",
        json={"project_id": str(sid("b-proj")), "format": fmt},
    )


def bibtex_entry_count(text: str) -> int:
    return len(re.findall(r"^@\w+\{", text, flags=re.MULTILINE))


@pytest.mark.parametrize("fmt", ["bibtex", "ieee", "apa", "mla"])
async def test_project_bibliography_member_gets_only_readable_documents(
    clients: Clients, bib_project: None, fmt: str
) -> None:
    """C gets c-proj-doc and nothing from the foreign or deleted links."""
    r = await export_project_bibliography(clients("C"), fmt)
    assert r.status_code == 200, r.text
    assert canary("c-proj-doc-title") in r.text  # positive control
    # The DOI only exists in document metadata: the fallback really ran.
    assert canary("c-proj-doc-doi") in r.text
    assert_no_canary(r.content, *HIDDEN_KEYS)
    if fmt == "bibtex":
        assert bibtex_entry_count(r.text) == 1


async def test_project_bibliography_with_only_unreadable_links_is_404(
    clients: Clients, bib_project: None, test_db: AsyncSession
) -> None:
    """With the readable row gone, C gets 404: no empty or counted entries."""
    await soft_delete(test_db, Document, "c-proj-doc")
    r = await export_project_bibliography(clients("C"), "bibtex")
    assert r.status_code == 404
    assert_no_canary(r.content, *PROJECT_KEYS)


async def test_project_bibliography_foreign_org_member_is_404(
    clients: Clients, bib_project: None, test_db: AsyncSession
) -> None:
    """A holds a live membership row in org-b's workspace, but project
    artifacts also require the workspace organization."""
    await add_member(test_db, "b", "A")
    r = await export_project_bibliography(clients("A"), "bibtex")
    assert r.status_code == 404
    assert_no_canary(r.content, *PROJECT_KEYS, "b-proj")


async def test_project_bibliography_removed_member_is_404(
    clients: Clients, bib_project: None
) -> None:
    c = clients("C")
    before = await export_project_bibliography(c, "bibtex")
    assert canary("c-proj-doc-title") in before.text  # access really existed

    r = await clients("B").delete(
        f"/api/v2/workspaces/{sid('b-ws')}/members/{sid('user-c')}"
    )
    assert r.status_code in (200, 204), r.text

    after = await export_project_bibliography(c, "bibtex")
    assert after.status_code == 404
    assert_no_canary(after.content, *PROJECT_KEYS, "b-proj")


@pytest.mark.parametrize(
    "document_key,organization_key,caller_key,allowed",
    [
        ("a-doc", "a", "user-a", True),
        ("b-doc", "b", "user-c", True),
        ("a-old-org-doc", "a", "user-a", False),
        ("a-old-org-doc", None, "user-a", False),
        ("a-doc", "a", None, False),
    ],
)
async def test_collection_document_access_fails_closed(
    seed: None,
    test_db: AsyncSession,
    document_key: str,
    organization_key: str | None,
    caller_key: str | None,
    allowed: bool,
) -> None:
    """An old uploader loses document access when organization scope is absent."""
    from src.services.threads.workspace_access import get_accessible_document_or_none

    result = await get_accessible_document_or_none(
        test_db,
        sid(document_key),
        sid(caller_key) if caller_key else None,
        sid(f"org-{organization_key}") if organization_key else None,
    )
    assert (result is not None) is allowed
    if result is not None:
        assert result.id == sid(document_key)


async def test_public_workspace_requires_identity_but_allows_cross_org_reader(
    seed: None, test_db: AsyncSession
) -> None:
    from src.models.workspace import Workspace
    from src.services.threads.workspace_access import user_can_access_workspace

    workspace = await test_db.get(Workspace, sid("b-ws"))
    assert workspace is not None
    workspace.is_public = True
    assert user_can_access_workspace(workspace, None) is False
    assert user_can_access_workspace(workspace, sid("user-a")) is True
