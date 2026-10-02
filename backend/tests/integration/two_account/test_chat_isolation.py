"""GOO-353: two-account chat reads and export revocation checks.

Matrix: docs/engineering/data-isolation-matrix.md (rows W*, X*). Workspace
access is membership/owner/public based, not organization based, so the
same-organization colleague C is a nonmember until invited.

``xfail(strict=True)`` rows are confirmed leaks owned by the named fix issue.
"""

import io
import zipfile
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ChatMessage, Conversation, MessageRole, Thread, Workspace
from tests.integration.two_account.conftest import (
    B_PRIVATE_CHAT_KEYS,
    Clients,
    add_member,
    assert_no_canary,
    canary,
    sid,
    soft_delete,
)

pytestmark = pytest.mark.integration


def chat_routes(key: str) -> list:
    """Every read route that can return ``key``'s workspace chain content."""
    ws, cv, th, msg = (sid(f"{key}-{x}") for x in ("ws", "conv", "thread", "msg"))
    nested = f"/api/v2/workspaces/{ws}/conversations/{cv}"
    return [
        f"/api/v2/workspaces/{ws}",
        f"/api/v2/workspaces/{ws}/conversations",
        f"/api/v2/workspaces/{ws}/threads",
        nested,
        f"{nested}/threads",
        f"{nested}/threads/{th}/messages",
        f"/api/v2/conversations/{cv}",
        f"/api/v2/conversations/{cv}/threads",
        # include_messages=true 500s today for any thread with messages
        # (MissingGreenlet lazy-loading citations in _format_message_response);
        # assert_denied still hits the default form, which denies before that.
        f"/api/v2/threads/{th}?include_messages=false",
        f"/api/v2/threads/{th}/messages",
        f"/api/v2/threads/{th}/messages/{msg}",
        f"/api/v2/threads/{th}/context",
        f"/api/v2/messages/{msg}",
    ]


def owner_only_routes(key: str) -> list:
    """Stricter than workspace access: workspace owner only (agent history),
    plus the thread detail with messages (see note in chat_routes)."""
    th = sid(f"{key}-thread")
    return [f"/api/v1/agent/threads/{th}/messages", f"/api/v2/threads/{th}"]


async def assert_denied(client: AsyncClient, key: str, *leak_keys: str) -> None:
    for route in chat_routes(key) + owner_only_routes(key):
        r = await client.get(route)
        # Thread-messages list answers 200 [] instead of 404; either way the
        # body must not carry the content.
        assert r.status_code in (404, 200), route
        if r.status_code == 200:
            assert r.json().get("messages") == [], route
        assert_no_canary(r.content, *(leak_keys or B_PRIVATE_CHAT_KEYS))
    listing = await client.get("/api/v2/workspaces")
    assert_no_canary(listing.content, f"{key}-ws")


async def assert_allowed(client: AsyncClient, key: str) -> None:
    for route in chat_routes(key):
        r = await client.get(route)
        assert r.status_code == 200, route
    r = await client.get(f"/api/v2/threads/{sid(f'{key}-thread')}/messages")
    assert canary(f"{key}-msg") in r.text


# --- W1-W4: who can read B's chat chain ----------------------------------


async def test_owner_reads_own_private_chain(clients: Clients) -> None:
    await assert_allowed(clients("B"), "b")
    r = await clients("B").get(owner_only_routes("b")[0])
    assert canary("b-msg") in r.text


async def test_cross_org_stranger_is_denied(clients: Clients) -> None:
    await assert_denied(clients("A"), "b")


async def test_same_org_nonmember_is_denied(clients: Clients) -> None:
    """Organization co-location does not grant workspace access."""
    await assert_denied(clients("C"), "b")


async def test_invited_member_reads_chain(
    clients: Clients, test_db: AsyncSession
) -> None:
    await add_member(test_db, "b", "C")
    await assert_allowed(clients("C"), "b")


async def test_public_workspace_is_readable_cross_org(clients: Clients) -> None:
    """Documented: a public workspace is visible to any authenticated user."""
    await assert_allowed(clients("A"), "b-pub")


# --- W5: deleted ancestors revoke every descendant ------------------------


@pytest.mark.parametrize(
    "model,key",
    [(Workspace, "b-ws"), (Conversation, "b-conv"), (Thread, "b-thread")],
)
async def test_deleted_ancestor_revokes_member(
    clients: Clients, test_db: AsyncSession, model: Any, key: str
) -> None:
    await add_member(test_db, "b", "C")
    await soft_delete(test_db, model, key)
    c = clients("C")
    for route in (
        f"/api/v2/threads/{sid('b-thread')}",
        f"/api/v2/threads/{sid('b-thread')}/messages/{sid('b-msg')}",
        f"/api/v2/messages/{sid('b-msg')}",
        f"/api/v2/threads/{sid('b-thread')}/context",
    ):
        r = await c.get(route)
        assert r.status_code == 404, route
        assert_no_canary(r.content, "b-msg")


async def test_deleted_ancestor_blocks_owner_export(
    clients: Clients, test_db: AsyncSession
) -> None:
    await soft_delete(test_db, Conversation, "b-conv")
    r = await clients("B").post(
        f"/api/v1/export/thread/{sid('b-thread')}", json={"format": "markdown"}
    )
    assert r.status_code == 404
    assert_no_canary(r.content, "b-msg")


# --- W6 + X*: membership removal revokes reads and exports ----------------

AFTER = "b-after-removal-msg"


@pytest.fixture
async def removed_member(clients: Clients, test_db: AsyncSession) -> AsyncClient:
    """A joins B's private workspace, starts a thread, is removed, then B
    appends a B-only message to that thread."""
    await add_member(test_db, "b", "A")
    test_db.add(
        Thread(
            id=sid("a-in-b-thread"),
            title="A thread inside B's workspace",
            conversation_id=sid("b-conv"),
            created_by_id=sid("user-a"),
            message_count=1,
        )
    )
    await test_db.commit()
    test_db.add(
        ChatMessage(
            id=sid("a-in-b-msg"),
            thread_id=sid("a-in-b-thread"),
            user_id=sid("user-a"),
            role=MessageRole.USER,
            content=canary("a-in-b-msg"),
        )
    )
    await test_db.commit()

    a = clients("A")
    before = await a.get(f"/api/v2/threads/{sid('a-in-b-thread')}/messages")
    assert canary("a-in-b-msg") in before.text  # member access really existed

    r = await clients("B").delete(
        f"/api/v2/workspaces/{sid('b-ws')}/members/{sid('user-a')}"
    )
    assert r.status_code in (200, 204), r.text

    test_db.add(
        ChatMessage(
            id=sid(AFTER),
            thread_id=sid("a-in-b-thread"),
            user_id=sid("user-b"),
            role=MessageRole.ASSISTANT,
            content=canary(AFTER),
        )
    )
    await test_db.commit()
    return a


async def test_removed_member_loses_reads(removed_member: AsyncClient) -> None:
    await assert_denied(removed_member, "b")
    th = sid("a-in-b-thread")
    for route in (f"/api/v2/threads/{th}", f"/api/v2/threads/{th}/messages"):
        r = await removed_member.get(route)
        assert_no_canary(r.content, AFTER, "b-msg")


async def test_creator_can_export_own_thread(clients: Clients) -> None:
    """Positive control: the export path does return content to its owner."""
    r = await clients("B").post(
        f"/api/v1/export/thread/{sid('b-thread')}", json={"format": "markdown"}
    )
    assert r.status_code == 200
    assert canary("b-msg") in r.text


EXPORT_FORMATS = ["markdown", "json", "html"]  # pdf needs a renderer: NOT RUN


@pytest.mark.parametrize("fmt", EXPORT_FORMATS)
async def test_removed_member_single_export(
    removed_member: AsyncClient, fmt: str
) -> None:
    r = await removed_member.post(
        f"/api/v1/export/thread/{sid('a-in-b-thread')}", json={"format": fmt}
    )
    assert_no_canary(r.content, AFTER)
    assert r.status_code == 404


@pytest.mark.parametrize("fmt", EXPORT_FORMATS)
async def test_removed_member_stream_export(
    removed_member: AsyncClient, fmt: str
) -> None:
    r = await removed_member.post(
        f"/api/v1/export/thread/{sid('a-in-b-thread')}/stream?format={fmt}"
    )
    assert_no_canary(r.content, AFTER)
    assert r.status_code == 404


async def test_removed_member_preview(removed_member: AsyncClient) -> None:
    r = await removed_member.post(f"/api/v1/export/preview/{sid('a-in-b-thread')}")
    assert r.status_code == 404


@pytest.mark.parametrize("fmt", EXPORT_FORMATS)
async def test_removed_member_batch_zip(removed_member: AsyncClient, fmt: str) -> None:
    """Batch packs authorized threads; the revoked one must be absent."""
    r = await removed_member.post(
        "/api/v1/export/batch",
        json={
            "thread_ids": [str(sid("a-in-b-thread")), str(sid("a-thread"))],
            "format": fmt,
            "as_zip": True,
        },
    )
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        body = b"".join(zf.read(n) for n in zf.namelist())
    assert canary("a-msg").encode() in body  # own thread still exported
    assert_no_canary(body, AFTER)


async def test_removed_member_batch_single(removed_member: AsyncClient) -> None:
    r = await removed_member.post(
        "/api/v1/export/batch",
        json={"thread_ids": [str(sid("a-in-b-thread"))], "as_zip": False},
    )
    assert_no_canary(r.content, AFTER)


@pytest.mark.parametrize("fmt", EXPORT_FORMATS)
async def test_stranger_export_is_denied(clients: Clients, fmt: str) -> None:
    """Single, stream and batch: B's thread never reaches cross-org A."""
    a, th = clients("A"), str(sid("b-thread"))
    responses = [
        await a.post(f"/api/v1/export/thread/{th}", json={"format": fmt}),
        await a.post(f"/api/v1/export/thread/{th}/stream?format={fmt}"),
        await a.post(
            "/api/v1/export/batch",
            json={"thread_ids": [th, str(sid("a-thread"))], "format": fmt},
        ),
    ]
    assert responses[0].status_code == 404
    assert responses[1].status_code == 404
    with zipfile.ZipFile(io.BytesIO(responses[2].content)) as zf:
        body = b"".join(zf.read(n) for n in zf.namelist())
    for r in responses[:2]:
        assert_no_canary(r.content, *B_PRIVATE_CHAT_KEYS)
    assert_no_canary(body, *B_PRIVATE_CHAT_KEYS)
