"""Two-account data-isolation fixtures (GOO-347).

The access matrix these fixtures exercise lives in
``docs/engineering/data-isolation-matrix.md``. Read it before adding a case.

Seed shape (every id is ``uuid5(NS, name)`` and every private string is a
``CANARY[name]`` constant, so a failing assertion names exactly which
account's data leaked and a rerun reproduces the same ids):

- ``A``  — organization ``org-a``. The caller under test.
- ``B``  — organization ``org-b``. Owns the private data A must never see.
- ``C``  — organization ``org-b`` (B's colleague). Same-org nonmember of B's
  private workspace; gets invited as a member by the revocation tests.

Data owned by B (org-b): a private document + public document (each with a
citation and a downloadable local file), a private workspace with
conversation/thread/message, and a public workspace with the same chain.
Data owned by A (org-a): one private document + citation and a private
workspace chain, for the positive own-data cases.

Every test runs against a fresh in-memory SQLite database (``test_db`` from
``tests/integration/conftest.py``), so cleanup is ``drop_all`` and is
idempotent by construction. No real users, organizations or deployed
databases are touched.

Authentication goes through the real ``MultiTenancyMiddleware`` and the real
``get_current_user`` dependency with an HS256 token per account — nothing
overrides the user resolution, so a client really is the account it claims.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.database import get_db
from src.main import app
from src.models import (
    ChatMessage,
    Citation,
    Conversation,
    Document,
    MessageRole,
    Organization,
    StorageTier,
    Thread,
    User,
    UserRole,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from src.models.document import DocumentType, ProcessingStatus

NS = uuid.UUID("5e1f0a3c-0b5d-4c6e-9f1a-2a7d3c4b5e60")
_SIGNING_KEY = "two-account-isolation-signing-key-not-for-production"


def sid(name: str) -> uuid.UUID:
    """Reproducible id for a seeded row."""
    return uuid.uuid5(NS, name)


def canary(name: str) -> str:
    """Synthetic marker that must only ever reach its owner's clients."""
    return f"ISO-CANARY-{name.upper()}-{sid(name).hex[:8]}"


Clients = Callable[[str], AsyncClient]


async def _add(db: AsyncSession, *rows: Any) -> None:
    db.add_all(rows)
    await db.commit()


def _org(name: str) -> Organization:
    return Organization(
        id=sid(f"org-{name}"),
        name=f"Isolation org {name}",
        storage_tier=StorageTier.FREE,
        storage_limit_bytes=Organization.get_default_storage_limit(StorageTier.FREE),
        is_active=True,
    )


def _user(name: str, org: str) -> User:
    return User(
        id=sid(f"user-{name}"),
        email=f"isolation-{name}@example.invalid",
        password_hash="unused",
        first_name=name.upper(),
        last_name="Isolation",
        role=UserRole.USER,
        is_active=True,
        organization_id=sid(f"org-{org}"),
    )


def _document(key: str, owner: str, org: str, tmp: Path, *, public: bool) -> Document:
    body = canary(f"{key}-content")
    path = tmp / f"{key}.txt"
    path.write_text(body)
    return Document(
        id=sid(key),
        title=canary(f"{key}-title"),
        filename=f"{key}.txt",
        file_path=str(path),
        file_size_bytes=len(body),
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        storage_backend="local",
        processing_status=ProcessingStatus.COMPLETED,
        content_text=body,
        is_public=public,
        uploaded_by_user_id=sid(f"user-{owner}"),
        organization_id=sid(f"org-{org}"),
    )


def _citation(key: str, document_key: str) -> Citation:
    return Citation(
        id=sid(key),
        document_id=sid(document_key),
        document_title=canary(f"{key}-title"),
        snippet=canary(f"{key}-quote"),
        metadata_source="manual",
    )


def _chat_chain(key: str, owner: str, org: str, *, public: bool) -> list:
    """Workspace -> conversation -> thread -> message, all owned by ``owner``.

    The owner also gets an OWNER membership row, as ``create_workspace`` does.
    """
    uid = sid(f"user-{owner}")
    return [
        Workspace(
            id=sid(f"{key}-ws"),
            name=canary(f"{key}-ws"),
            owner_id=uid,
            organization_id=sid(f"org-{org}"),
            is_public=public,
        ),
        WorkspaceMember(
            id=sid(f"member-{key}-{owner}"),
            workspace_id=sid(f"{key}-ws"),
            user_id=uid,
            role=WorkspaceRole.OWNER,
        ),
        Conversation(
            id=sid(f"{key}-conv"),
            title=canary(f"{key}-conv"),
            workspace_id=sid(f"{key}-ws"),
            created_by_id=uid,
        ),
        Thread(
            id=sid(f"{key}-thread"),
            title=canary(f"{key}-thread"),
            conversation_id=sid(f"{key}-conv"),
            created_by_id=uid,
            message_count=1,
        ),
        ChatMessage(
            id=sid(f"{key}-msg"),
            thread_id=sid(f"{key}-thread"),
            user_id=uid,
            role=MessageRole.USER,
            content=canary(f"{key}-msg"),
        ),
    ]


@pytest_asyncio.fixture
async def seed(test_db: AsyncSession, tmp_path: Path) -> None:
    """Seed A/B/C plus their documents, citations and chat chains."""
    await _add(test_db, _org("a"), _org("b"))
    users = {"A": _user("a", "a"), "B": _user("b", "b"), "C": _user("c", "b")}
    await _add(test_db, *users.values())
    await _add(
        test_db,
        _document("a-doc", "a", "a", tmp_path, public=False),
        _document("b-doc", "b", "b", tmp_path, public=False),
        _document("b-pub-doc", "b", "b", tmp_path, public=True),
        # Uploaded by A while A belonged to org-b; A has since moved to org-a.
        _document("a-old-org-doc", "a", "b", tmp_path, public=False),
    )
    await _add(
        test_db,
        _citation("a-cit", "a-doc"),
        _citation("b-cit", "b-doc"),
        _citation("b-pub-cit", "b-pub-doc"),
        _citation("a-old-org-cit", "a-old-org-doc"),
    )
    for rows in (
        _chat_chain("a", "a", "a", public=False),
        _chat_chain("b", "b", "b", public=False),
        _chat_chain("b-pub", "b", "b", public=True),
    ):
        for row in rows:  # parent before child: SQLite enforces nothing, PG does
            await _add(test_db, row)


async def add_member(db: AsyncSession, workspace_key: str, who: str) -> None:
    """Invite ``who`` into ``workspace_key`` as an editor (DB-level setup)."""
    await _add(
        db,
        WorkspaceMember(
            id=sid(f"member-{workspace_key}-{who}"),
            workspace_id=sid(f"{workspace_key}-ws"),
            user_id=sid(f"user-{who.lower()}"),
            role=WorkspaceRole.EDITOR,
        ),
    )


def _token(user_id: uuid.UUID) -> str:
    return str(
        jwt.encode(
            {
                "sub": str(user_id),
                "aud": "authenticated",
                "app_metadata": {},
                "exp": int(time.time()) + 3600,
            },
            _SIGNING_KEY,
            algorithm="HS256",
        )
    )


@pytest_asyncio.fixture
async def clients(
    test_db: AsyncSession, seed: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Clients]:
    """``clients("A")`` -> an HTTP client authenticated as account A.

    Only ``get_db`` is overridden (to share the in-memory engine). Token
    verification, user lookup and the tenancy middleware are the real code.
    """
    from src.core.config import settings
    from src.middleware import multi_tenancy

    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", _SIGNING_KEY)
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", "")

    # A fresh session per request, like production: sharing ``test_db``
    # across requests would serve stale identity-map rows (e.g. a removed
    # membership) and hide or fake an access decision.
    session_factory = async_sessionmaker(test_db.bind, expire_on_commit=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", session_factory)
    app.dependency_overrides[get_db] = override_get_db
    opened: Dict[str, AsyncClient] = {}

    def _client(who: str) -> AsyncClient:
        if who not in opened:
            opened[who] = AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://localhost",
                headers={
                    "Authorization": f"Bearer {_token(sid(f'user-{who.lower()}'))}"
                },
            )
        return opened[who]

    try:
        yield _client
    finally:
        for c in opened.values():
            await c.aclose()
        app.dependency_overrides.clear()


def assert_no_canary(body: bytes | str, *keys: str) -> None:
    """Fail with the leaked key's name, never the full response."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    leaked = [k for k in keys if canary(k) in text]
    assert not leaked, f"leaked canaries: {leaked}"


B_PRIVATE_CHAT_KEYS = ("b-ws", "b-conv", "b-thread", "b-msg")


async def soft_delete(db: AsyncSession, model: Any, key: str) -> None:
    """Soft-delete one seeded row with a plain UPDATE (no identity-map state)."""
    await db.execute(update(model).where(model.id == sid(key)).values(is_deleted=True))
    await db.commit()
