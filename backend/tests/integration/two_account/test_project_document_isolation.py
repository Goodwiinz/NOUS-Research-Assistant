"""GOO-410: two-account checks for project document links and the project
bibliography (``backend/src/api/research/projects.py``).

Matrix: docs/engineering/data-isolation-matrix.md (rows PD*). Every request
goes through an authenticated HTTP client; nothing mocks a guard.

Adding a document to a project has no uploader-or-public check any more
(documents are organization-shared), so its org + not-deleted lookup is the
whole document guard: PD1 and PD2 pin both halves of it, PD1+ the
organization-shared positive.

A project can still hold a foreign-org document: the v2 collections route
lets a cross-org workspace editor link a document from their own
organization. The project bibliography must then skip it, in its citation
read (PD3) and in its document-metadata fallback (PD4), as
``list_project_documents`` and the citations export already do.

Mutation checks (each fails at least one test here):
- drop ``Document.organization_id == current_user.organization_id`` from the
  ``add_document_to_project`` lookup: PD1 links org-b documents (201);
- drop ``Document.is_deleted == False`` from it: PD2 links b-doc (201);
- drop the organization predicate from the bibliography citation query: PD3
  returns a-cit; from the fallback query: PD4 returns a-doc's title.
"""

from typing import Set
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Citation, Collection, CollectionDocument, Document
from tests.integration.two_account.conftest import (
    Clients,
    add_member,
    assert_no_canary,
    canary,
    sid,
    soft_delete,
)

pytestmark = pytest.mark.integration

A_PROJECT = "a-project"  # in A's private workspace ``a`` (org-a)
B_PROJECT = "b-project"  # in B's private workspace ``b`` (org-b)
A_DOC_KEYS = ("a-doc-title", "a-doc-content", "a-cit-title", "a-cit-quote")


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linking a document queues a KG extraction after commit. CI runs a real
    Redis, so keep that off the broker: these tests only check access."""
    from src.tasks import enqueue

    monkeypatch.setattr(
        enqueue, "enqueue_after_commit_apply_async", lambda *args, **kwargs: None
    )


@pytest_asyncio.fixture
async def projects(test_db: AsyncSession, seed: None) -> None:
    """One project in A's workspace ``a`` and one in B's workspace ``b``."""
    test_db.add_all(
        [
            Collection(id=sid(key), name=canary(key), workspace_id=sid(f"{ws}-ws"))
            for key, ws in ((A_PROJECT, "a"), (B_PROJECT, "b"))
        ]
    )
    await test_db.commit()


async def _links(db: AsyncSession, project: str) -> Set[UUID]:
    """Live document links of ``project``, read straight from the database."""
    rows = await db.execute(
        select(CollectionDocument.document_id).where(
            CollectionDocument.collection_id == sid(project),
            CollectionDocument.is_deleted.is_(False),
        )
    )
    return set(rows.scalars().all())


def _add_url(project: str, doc: str) -> str:
    return f"/api/v1/projects/{sid(project)}/documents?document_id={sid(doc)}"


def _bibliography_url(project: str) -> str:
    return f"/api/v1/projects/{sid(project)}/bibliography"


# --- PD1: adding a document follows the document read boundary ------------


@pytest.mark.parametrize("doc", ["b-doc", "b-pub-doc", "a-old-org-doc"])
async def test_add_foreign_org_document_to_project_is_404(
    clients: Clients, projects: None, test_db: AsyncSession, doc: str
) -> None:
    """A edits a-project but cannot link an org-b document: private, public,
    or one A uploaded while it belonged to org-b."""
    r = await clients("A").post(_add_url(A_PROJECT, doc))
    assert r.status_code == 404
    assert_no_canary(r.content, f"{doc}-title", f"{doc}-content")
    assert await _links(test_db, A_PROJECT) == set()


async def test_colleague_adds_private_document_to_project(
    clients: Clients, projects: None, test_db: AsyncSession
) -> None:
    """PD1+: C (org-b, invited as editor of ``b``) links B's private b-doc;
    documents are organization-shared, as in D2."""
    await add_member(test_db, "b", "C")
    r = await clients("C").post(_add_url(B_PROJECT, "b-doc"))
    assert r.status_code == 201
    assert await _links(test_db, B_PROJECT) == {sid("b-doc")}


# --- PD2: a soft-deleted document cannot be linked -------------------------


@pytest.mark.parametrize("who", ["B", "C"])
async def test_add_deleted_document_to_project_is_404(
    clients: Clients, projects: None, test_db: AsyncSession, who: str
) -> None:
    """Neither the uploader B nor the colleague C can link b-doc once it is
    soft-deleted."""
    await add_member(test_db, "b", "C")
    await soft_delete(test_db, Document, "b-doc")
    r = await clients(who).post(_add_url(B_PROJECT, "b-doc"))
    assert r.status_code == 404
    assert_no_canary(r.content, "b-doc-title", "b-doc-content")
    assert await _links(test_db, B_PROJECT) == set()


# --- PD3/PD4: the project bibliography stays inside the organization -------


@pytest_asyncio.fixture
async def mixed_project(
    clients: Clients, projects: None, test_db: AsyncSession
) -> None:
    """b-project links B's b-doc and A's org-a a-doc.

    A, invited as an editor of ``b`` (workspace membership may cross
    organizations), links a-doc through the v2 collections route, which
    accepts any document of the caller's own organization."""
    await add_member(test_db, "b", "A")
    r = await clients("A").post(
        f"/api/v2/collections/{sid(B_PROJECT)}/documents",
        json={"document_ids": [str(sid("a-doc"))]},
    )
    assert r.status_code == 200
    r = await clients("B").post(_add_url(B_PROJECT, "b-doc"))
    assert r.status_code == 201
    assert await _links(test_db, B_PROJECT) == {sid("a-doc"), sid("b-doc")}


async def test_project_bibliography_skips_foreign_org_citation(
    clients: Clients, mixed_project: None
) -> None:
    """PD3: B gets b-cit and nothing from a-doc. A, the cross-org editor,
    cannot read the project's bibliography at all."""
    r = await clients("B").get(_bibliography_url(B_PROJECT))
    assert r.status_code == 200
    assert r.json()["citation_count"] == 1
    assert canary("b-cit-title") in r.text
    assert_no_canary(r.content, *A_DOC_KEYS)

    r = await clients("A").get(_bibliography_url(B_PROJECT))
    assert r.status_code == 404
    assert_no_canary(r.content, "b-cit-title", "b-doc-title", *A_DOC_KEYS)


async def test_project_bibliography_fallback_skips_foreign_org_document(
    clients: Clients, mixed_project: None, test_db: AsyncSession
) -> None:
    """PD4: with b-cit soft-deleted no readable citation row is left, so the
    route builds entries from document metadata. That fallback lists b-doc
    (positive control) and still skips a-doc."""
    await soft_delete(test_db, Citation, "b-cit")
    r = await clients("B").get(_bibliography_url(B_PROJECT))
    assert r.status_code == 200
    assert r.json()["citation_count"] == 1
    assert canary("b-doc-title") in r.text
    assert_no_canary(r.content, "b-cit-title", *A_DOC_KEYS)


async def test_project_bibliography_skips_deleted_document(
    clients: Clients, mixed_project: None, test_db: AsyncSession
) -> None:
    """PD4: once b-doc is soft-deleted, neither its citation nor its metadata
    reaches the bibliography; with a-doc foreign, nothing is left."""
    await soft_delete(test_db, Document, "b-doc")
    r = await clients("B").get(_bibliography_url(B_PROJECT))
    assert r.status_code == 200
    assert r.json()["citation_count"] == 0
    assert_no_canary(r.content, "b-cit-title", "b-doc-title", *A_DOC_KEYS)
