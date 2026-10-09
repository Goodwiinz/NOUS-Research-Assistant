r"""Plan 06 acceptance journey on real PostgreSQL: a second harness session
continues the first one's work in the same NOUS project and chat.

Everything runs in-process (no model, no bridge): consent over the grant
routes, project reads over the integration read gateway, publication through
the artifact service with the grant's resolved context, handoffs over the
integration and thread routes, and a fresh browser identity over the artifact
and thread routes.

Focused command, from the repository root (``PY`` and the throwaway PostgreSQL
come from Gate 0 of ``docs/testing/harness-live-proof.md``)::

    ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch \
    ENVIRONMENT=testing PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
    backend/tests/integration/test_same_project_continuation.py

Without the URL the test is skipped; a skip is NOT RUN, never PASS. Hosted CI
sets no URL.

The integration conftest mocks Redis, so the CLI-bearer cutoff (401) cannot be
shown here; revocation is proven on the grant (403).

Mutation verification (2026-10-05, local PostgreSQL 14.23), source restored
after each:

* deleting ``uq_integration_handoffs_thread_project_version`` from
  ``IntegrationHandoff.__table_args__`` lets both concurrent v3 saves commit:
  the race assertion FAILS with ``[200, 200] == [200, 409]``;
* deleting the ``_replayed`` short-circuit at the top of
  ``services/integrations/handoffs.py::save`` turns the identical replay of v2
  into a 409: the replay assertion FAILS (``409 == 200``);
* deleting the ``expected_parent_version`` check in ``save`` accepts a save
  naming a parent that does not exist yet: the ahead-parent assertion FAILS
  (``200 == 409``). A stale parent alone does not catch it, because the unique
  key refuses the reused version number;
* adding the version, its reference and its lifecycle outbox row in one flush
  in ``services/artifacts/service.py::publish_version`` (the code before this
  test) makes the first publish raise ``ArtifactConflict``: these models
  declare no ``relationship()``, so the unit of work inserted
  ``artifact_lifecycle_outbox`` before ``artifact_versions`` and PostgreSQL
  refused the foreign key. SQLite unit tests do not enforce it.
* restoring ``Workspace.organization_id == organization_id`` in
  ``services/integrations/handoffs.py::_live_chain`` (the code before WG-2c)
  makes ``[legacy-null-org]`` FAIL at step 3's card GET (``404 == 200``)
  while ``[org]`` passes (2026-10-08, local PostgreSQL 14.23).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.artifacts import router as artifacts_router
from src.api.integrations import router as integrations_router
from src.api.threads.workspace_routes import threads as thread_routes
from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.models.agent_run import AgentRun
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.artifact import (
    Artifact,
    ArtifactLifecycleOutbox,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.base import Base
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection, CollectionDocument
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.integration_handoff import IntegrationHandoff
from src.models.organization import Organization
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.artifact import ArtifactConflict, ArtifactVersionDTO
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import service as artifact_service
from src.services.artifacts.editing import edit_version
from src.services.artifacts.storage import MemoryArtifactStorage
from src.services.integrations import handoffs
from src.services.integrations.context import resolve_integration_context
from tests.utils.artifact_publication import (
    publish_content,
    publish_request,
    reserve_request,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]

_MODELS: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    ResearchProject,
    ResearchProjectRoleAssignment,
    Document,
    CollectionDocument,
    Conversation,
    Thread,
    ChatMessage,
    AgentRuntimeSnapshot,
    AgentRun,
    BridgeDevice,
    WorkspaceBinding,
    IntegrationGrantRequest,
    IntegrationGrant,
    ArtifactUpload,
    Artifact,
    ArtifactVersion,
    ArtifactReference,
    ArtifactLifecycleOutbox,
    IntegrationHandoff,
]
SCOPES = ["tools:read", "artifacts:publish", "handoff:read", "handoff:write"]
AS = "x-test-as"  # which identity the overridden auth dependencies return
V1 = "/api/v1"
FILES: dict[str, tuple[bytes, str]] = {
    "analysis.py": (b"import csv\nprint('rows')\n", "text/x-python"),
    "data.csv": (b"x,y\n1,2\n3,4\n", "text/csv"),
    "plot.png": (b"\x89PNG\r\n\x1a\nfirst-plot", "image/png"),
}
PLOT_V2 = b"\x89PNG\r\n\x1a\nsecond-plot"


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    return dsn


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _same(left: Any, right: Any) -> bool:
    """Handoff DTO equality. A save answers with the ORM default
    ``datetime.utcnow`` (naive) while a read answers with the stored aware
    value, so ``created_at`` is compared as an instant."""
    if left is None or right is None:
        return left is right
    return {**left, "created_at": _instant(left["created_at"])} == {
        **right,
        "created_at": _instant(right["created_at"]),
    }


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _cli(token: str | None = None) -> dict[str, str]:
    headers = {AS: "cli"}
    if token is not None:
        headers["X-NOUS-Integration-Grant"] = token
    return headers


BROWSER = {AS: "browser"}
OUTSIDER = {AS: "outsider"}


def _handoff(
    parent: int | None, results: list[ArtifactVersionDTO], **values: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "handoff_id": str(uuid.uuid4()),
        "expected_parent_version": parent,
        "goal": "Reproduce the figure from the project data",
        "decisions": ["Use the CSV export"],
        "remaining": ["Label the axes"],
        "results": [
            {"artifact_version_id": str(v.version_id), "summary": v.title}
            for v in results
        ],
        "harness_name": "codex",
        "harness_session_id": "session-1",
    }
    body.update(values)
    return body


async def _seed(
    factory: async_sessionmaker[AsyncSession], ids: SimpleNamespace
) -> None:
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO organizations (id, created_at, updated_at,
                is_deleted, name, storage_tier, storage_used_bytes,
                storage_limit_bytes, is_active) VALUES (:id, now(), now(),
                false, 'org', 'FREE', 0, 1000000, true)"""),
            {"id": ids.org},
        )
        for user in (ids.owner, ids.outsider):
            await db.execute(
                text("""INSERT INTO users (id, created_at, updated_at, is_deleted,
                    email, password_hash, first_name, last_name, role, is_active,
                    organization_id, login_count) VALUES (:id, now(), now(), false,
                    :email, 'unused', 'x', 'y', 'USER', true, :org, 0)"""),
                {"id": user, "email": f"{user}@example.test", "org": ids.org},
            )
        # One flush per parent level: the unit of work cannot order these FKs.
        rows: list[list[Any]] = [
            [
                Workspace(
                    id=ids.workspace,
                    name="w",
                    owner_id=ids.owner,
                    # WG-2: a legacy workspace carries no organization and is
                    # its owner's.
                    organization_id=None if ids.legacy else ids.org,
                )
            ],
            [
                Collection(id=ids.project, workspace_id=ids.workspace, name="P"),
                Collection(id=ids.other_project, workspace_id=ids.workspace, name="Q"),
                Conversation(
                    id=ids.conversation,
                    workspace_id=ids.workspace,
                    title="c",
                    created_by_id=ids.owner,
                ),
            ]
            + [
                Document(
                    id=doc,
                    title=f"Source {n}",
                    filename=f"source-{n}.txt",
                    file_path=f"local:///nonexistent/source-{n}.txt",
                    file_size_bytes=10,
                    mime_type="text/plain",
                    document_type=DocumentType.TEXT,
                    organization_id=ids.org,
                    uploaded_by_user_id=ids.owner,
                    processing_status=ProcessingStatus.COMPLETED,
                )
                for n, doc in enumerate(ids.documents)
            ],
            [
                Thread(
                    id=ids.thread,
                    conversation_id=ids.conversation,
                    source_project_id=ids.project,
                    title="Figure work",
                    created_by_id=ids.owner,
                    message_count=0,
                )
            ]
            + [
                CollectionDocument(collection_id=ids.project, document_id=doc)
                for doc in ids.documents
            ],
        ]
        for level in rows:
            db.add_all(level)
            await db.flush()
        await db.commit()


def _app(factory: async_sessionmaker[AsyncSession], ids: SimpleNamespace) -> FastAPI:
    def who(request: Request) -> tuple[uuid.UUID, bool]:
        role = request.headers.get(AS, "browser")
        return (ids.outsider if role == "outsider" else ids.owner), role == "cli"

    async def db_override() -> AsyncIterator[AsyncSession]:
        # A fresh session per request, so concurrent requests are two sessions.
        async with factory() as session:
            yield session

    def token_override(request: Request) -> TokenData:
        user, is_cli = who(request)
        return TokenData(user_id=str(user), organization_id=str(ids.org), is_cli=is_cli)

    def user_override(request: Request) -> SimpleNamespace:
        return SimpleNamespace(id=who(request)[0], organization_id=ids.org)

    app = FastAPI()
    app.include_router(artifacts_router, prefix=V1)
    app.include_router(integrations_router, prefix=V1)
    app.include_router(thread_routes.standalone_router)
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user_token] = token_override
    app.dependency_overrides[get_current_user] = user_override
    return app


async def _consent(
    client: httpx.AsyncClient, project: uuid.UUID, thread: uuid.UUID | None
) -> dict[str, Any]:
    """Device pairing, chat-bound consent, browser approval, CLI exchange."""
    device = await client.post(
        f"{V1}/integrations/devices", json={"label": "laptop"}, headers=_cli()
    )
    assert device.status_code == 200, device.text
    created = await client.post(
        f"{V1}/integrations/grant-requests",
        json={
            "project_id": str(project),
            "device_id": device.json()["id"],
            "scopes": SCOPES,
            "thread_id": str(thread) if thread else None,
        },
        headers=_cli(),
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]
    decided = await client.post(
        f"{V1}/integrations/grant-requests/{request_id}/decision",
        json={"approved": True},
        headers=BROWSER,
    )
    assert decided.status_code == 200, decided.text
    issued = await client.post(
        f"{V1}/integrations/grant-requests/{request_id}/exchange", headers=_cli()
    )
    assert issued.status_code == 200, issued.text
    return cast(dict[str, Any], issued.json())


async def _context(
    factory: async_sessionmaker[AsyncSession], token: str
) -> IntegrationContext:
    async with factory() as db:
        return await resolve_integration_context(
            db, token, required_scope="artifacts:publish"
        )


async def _publish(
    factory: async_sessionmaker[AsyncSession],
    context: IntegrationContext,
    title: str,
    content: bytes,
    mime_type: str,
    **overrides: Any,
) -> ArtifactVersionDTO:
    async with factory() as db:
        _, version = await publish_content(
            db, context, content=content, mime_type=mime_type, title=title, **overrides
        )
    assert version.sha256 == _sha(content)
    return version


async def _publication_survives_renewal(
    client: httpx.AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    ids: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-flight reservations across renewal retain one receipt."""
    issued = await _consent(client, ids.project, ids.thread)
    before = await _context(factory, issued["token"])
    replacement = await client.post(
        f"{V1}/integrations/grants/{issued['grant_id']}/renew",
        headers=_cli(issued["token"]),
    )
    assert replacement.status_code == 200, replacement.text
    after = await _context(factory, replacement.json()["token"])
    assert before.grant_id != after.grant_id
    assert before.consent_id == after.consent_id
    request = reserve_request()
    arrived = asyncio.Event()
    reads = 0
    original_lookup = artifact_service._existing_reservation

    async def concurrent_lookup(
        db: AsyncSession, context: IntegrationContext, publication_id: uuid.UUID
    ) -> ArtifactUpload | None:
        nonlocal reads
        result = await original_lookup(db, context, publication_id)
        reads += 1
        if reads <= 2:
            if reads == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=10)
        return result

    async def reserve(context: IntegrationContext) -> Any:
        async with factory() as db:
            result = await artifact_service.reserve_upload(db, context, request)
            # Returning a concurrent replay must release its service-owned
            # project lock while the caller keeps this session alive.
            assert not db.in_transaction()
            return result

    # A context resolved before renewal can already have a request in flight;
    # renewal changes the bearer, never the consent's publication identity.
    with monkeypatch.context() as patcher:
        patcher.setattr(artifact_service, "_existing_reservation", concurrent_lookup)
        first, replay = await asyncio.wait_for(
            asyncio.gather(reserve(before), reserve(after)), timeout=15
        )
    assert first.upload_id == replay.upload_id
    publication = publish_request(request, first.upload_id)
    async with factory() as db:
        await artifact_service.store_upload(db, after, first.upload_id, b"report\n")
        version = await artifact_service.publish_version(db, after, publication)
    async with factory() as db:
        retried = await artifact_service.publish_version(db, after, publication)
        count = await db.scalar(
            select(func.count())
            .select_from(ArtifactUpload)
            .where(ArtifactUpload.publication_id == request.publication_id)
        )
    assert retried.version_id == version.version_id
    assert count == 1
    await _editing_is_conflict_safe(factory, after, version, monkeypatch)


async def _editing_is_conflict_safe(
    factory: async_sessionmaker[AsyncSession],
    context: IntegrationContext,
    original: ArtifactVersionDTO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both editors observe the old parent before either may commit."""
    storage = artifact_service.get_artifact_storage()
    original_exists = storage.exists
    observed, both_observed = 0, asyncio.Event()

    async def synchronized_exists(key: str) -> bool:
        nonlocal observed
        result = await original_exists(key)
        observed += 1
        if observed == 2:
            both_observed.set()
        await asyncio.wait_for(both_observed.wait(), timeout=10)
        return result

    async def edit(content: str) -> ArtifactVersionDTO | ArtifactConflict:
        async with factory() as db:
            try:
                return await edit_version(
                    db,
                    user_id=context.user_id,
                    organization_id=context.organization_id,
                    artifact_id=original.artifact_id,
                    expected_parent_version_id=original.version_id,
                    publication_id=uuid.uuid4(),
                    text=content,
                )
            except ArtifactConflict as error:
                return error

    with monkeypatch.context() as patcher:
        patcher.setattr(storage, "exists", synchronized_exists)
        outcomes = await asyncio.wait_for(
            asyncio.gather(edit("first"), edit("second")), timeout=15
        )
    assert sum(isinstance(item, ArtifactVersionDTO) for item in outcomes) == 1
    assert sum(isinstance(item, ArtifactConflict) for item in outcomes) == 1
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ArtifactVersion)
                .where(ArtifactVersion.artifact_id == original.artifact_id)
            )
            == 2
        )
        content, _, _ = await artifact_service.read_version_content(
            db,
            user_id=context.user_id,
            organization_id=context.organization_id,
            version_id=original.version_id,
        )
        assert content == b"report\n"


async def _journey(
    client: httpx.AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    ids: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Consent A bound to project P and chat T; the read gateway sees P.
    grant_a = await _consent(client, ids.project, ids.thread)
    listed = await client.post(
        f"{V1}/integrations/tools/read",
        json={
            "tool_name": "list_project_documents",
            "arguments": {},
            "invocation_id": str(uuid.uuid4()),
        },
        headers=_cli(grant_a["token"]),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["is_error"] is False
    assert {ref["document_id"] for ref in listed.json()["source_refs"]} == {
        str(doc) for doc in ids.documents
    }

    # 2. Session A publishes three files and leaves handoff v1.
    context_a = await _context(factory, grant_a["token"])
    assert context_a.thread_id == ids.thread
    published = {
        title: await _publish(factory, context_a, title, content, mime)
        for title, (content, mime) in FILES.items()
    }
    saved = await client.post(
        f"{V1}/integrations/handoffs",
        json=_handoff(None, list(published.values())),
        headers=_cli(grant_a["token"]),
    )
    assert saved.status_code == 200, saved.text
    v1 = saved.json()
    assert v1["version"] == 1

    # 3. A fresh browser finds all three from the project id alone.
    project = await client.get(
        f"{V1}/artifacts/projects/{ids.project}", headers=BROWSER
    )
    assert project.status_code == 200, project.text
    rows = {row["title"]: row for row in project.json()}
    assert set(rows) == set(FILES)
    for title, row in rows.items():
        content, mime = FILES[title]
        assert row["thread_id"] == str(ids.thread)
        assert row["current_version"]["sha256"] == _sha(content)
        assert row["kind"] == mime
        download = await client.get(
            f"{V1}/artifacts/versions/{row['current_version']['version_id']}/content",
            headers=BROWSER,
        )
        assert download.status_code == 200
        assert _sha(download.content) == row["current_version"]["sha256"]
        async with factory() as db:
            stored = await artifact_service.read_version_content(
                db,
                user_id=ids.owner,
                organization_id=ids.org,
                version_id=uuid.UUID(row["current_version"]["version_id"]),
            )
        assert _sha(stored[0]) == row["current_version"]["sha256"]
    card = await client.get(f"/api/v2/threads/{ids.thread}/handoff", headers=BROWSER)
    assert card.status_code == 200, card.text
    assert _same(card.json(), v1)

    # 4. Session B on a new device continues the same project and chat.
    grant_b = await _consent(client, ids.project, ids.thread)
    latest = await client.get(
        f"{V1}/integrations/handoffs/latest", headers=_cli(grant_b["token"])
    )
    assert latest.status_code == 200 and _same(latest.json(), v1)
    context_b = await _context(factory, grant_b["token"])
    plot_v1 = published["plot.png"]
    plot_v2 = await _publish(
        factory,
        context_b,
        "plot.png",
        PLOT_V2,
        "image/png",
        artifact_id=plot_v1.artifact_id,
        expected_parent_version_id=plot_v1.version_id,
    )
    assert plot_v2.artifact_id == plot_v1.artifact_id
    assert plot_v2.parent_version_id == plot_v1.version_id
    history = await client.get(
        f"{V1}/artifacts/{plot_v1.artifact_id}/versions", headers=BROWSER
    )
    assert [(v["version_id"], v["sha256"]) for v in history.json()] == [
        (str(plot_v1.version_id), plot_v1.sha256),
        (str(plot_v2.version_id), plot_v2.sha256),
    ]
    first = await client.get(
        f"{V1}/artifacts/versions/{plot_v1.version_id}/content", headers=BROWSER
    )
    assert _sha(first.content) == _sha(FILES["plot.png"][0])
    v2_body = _handoff(1, [plot_v2], harness_session_id="session-2")
    saved = await client.post(
        f"{V1}/integrations/handoffs", json=v2_body, headers=_cli(grant_b["token"])
    )
    assert saved.status_code == 200, saved.text
    v2 = saved.json()
    assert v2["version"] == 2

    # 5. Denials and races.
    # An unbound grant (project Q, no chat) has no handoff chain to read or write.
    grant_q = await _consent(client, ids.other_project, None)
    unbound = _cli(grant_q["token"])
    assert (
        await client.get(f"{V1}/integrations/handoffs/latest", headers=unbound)
    ).status_code == 403
    assert (
        await client.post(
            f"{V1}/integrations/handoffs", json=_handoff(2, []), headers=unbound
        )
    ).status_code == 403

    # A revoked grant can no longer save.
    revoked = await client.delete(
        f"{V1}/integrations/grants/{grant_a['grant_id']}", headers=BROWSER
    )
    assert revoked.status_code == 204, revoked.text
    stale_a = await client.post(
        f"{V1}/integrations/handoffs",
        json=_handoff(2, []),
        headers=_cli(grant_a["token"]),
    )
    assert stale_a.status_code == 403

    # Another user of the same organization sees neither files nor card.
    assert (
        await client.get(f"{V1}/artifacts/projects/{ids.project}", headers=OUTSIDER)
    ).status_code == 404
    assert (
        await client.get(f"/api/v2/threads/{ids.thread}/handoff", headers=OUTSIDER)
    ).status_code == 404

    # Replaying v2's handoff_id with the identical body returns the same row.
    replay = await client.post(
        f"{V1}/integrations/handoffs", json=v2_body, headers=_cli(grant_b["token"])
    )
    assert replay.status_code == 200, replay.text
    assert _same(replay.json(), v2)

    # A result from another project is refused before anything is stored.
    context_q = await _context(factory, grant_q["token"])
    foreign = await _publish(factory, context_q, "q.md", b"q\n", "text/markdown")
    wrong = await client.post(
        f"{V1}/integrations/handoffs",
        json=_handoff(2, [foreign]),
        headers=_cli(grant_b["token"]),
    )
    assert wrong.status_code == 422, wrong.text

    # Two sessions both read v2 and save v3: exactly one wins.
    barrier = asyncio.Barrier(2)
    check_results = handoffs._check_results

    async def both_validated(
        db: AsyncSession, context: IntegrationContext, payload: Any
    ) -> None:
        # Both writers have passed the parent check before either inserts.
        await asyncio.wait_for(barrier.wait(), timeout=10)
        await check_results(db, context, payload)

    monkeypatch.setattr(handoffs, "_check_results", both_validated)
    bodies = [_handoff(2, [], goal=f"writer {n}") for n in range(2)]

    async def post(body: dict[str, Any]) -> httpx.Response:
        return await client.post(
            f"{V1}/integrations/handoffs", json=body, headers=_cli(grant_b["token"])
        )

    responses = await asyncio.gather(*(post(body) for body in bodies))
    monkeypatch.setattr(handoffs, "_check_results", check_results)
    assert sorted(r.status_code for r in responses) == [200, 409], [
        r.text for r in responses
    ]
    winner = next(r for r in responses if r.status_code == 200).json()
    loser = next(r for r in responses if r.status_code == 409).json()
    assert winner["version"] == 3
    assert _same(loser["latest"], winner)
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(IntegrationHandoff)
                .where(IntegrationHandoff.version == 3)
            )
        ) == 1

    # A later save on the stale parent gets the latest version back.
    stale = await client.post(
        f"{V1}/integrations/handoffs",
        json=_handoff(2, []),
        headers=_cli(grant_b["token"]),
    )
    assert stale.status_code == 409
    assert _same(stale.json()["latest"], winner)
    # A parent that does not exist yet would leave a gap; only the parent
    # check refuses it (the unique key cannot, version 8 is free).
    ahead = await client.post(
        f"{V1}/integrations/handoffs",
        json=_handoff(7, []),
        headers=_cli(grant_b["token"]),
    )
    assert ahead.status_code == 409
    assert _same(ahead.json()["latest"], winner)


@pytest.mark.parametrize("legacy", [False, True], ids=["org", "legacy-null-org"])
async def test_second_session_continues_the_same_project_chat(
    monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    # `or ""` keeps dsn a `str` without leaning on pytest.skip being NoReturn.
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    # asyncpg stores the naive ``datetime.utcnow`` ORM defaults in the process
    # zone; pods run in UTC, so the journey does too.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)
    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", True)
    storage = MemoryArtifactStorage()
    monkeypatch.setattr(artifact_service, "get_artifact_storage", lambda: storage)

    schema = "same_project_" + uuid.uuid4().hex
    admin = create_async_engine(_async_dsn(dsn))
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        _async_dsn(dsn),
        connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}},
    )
    try:
        async with engine.begin() as connection:
            tables = [cast(Any, model).__table__ for model in _MODELS]
            await connection.run_sync(Base.metadata.create_all, tables=tables)
        ids = SimpleNamespace(
            org=uuid.uuid4(),
            owner=uuid.uuid4(),
            outsider=uuid.uuid4(),
            workspace=uuid.uuid4(),
            project=uuid.uuid4(),
            other_project=uuid.uuid4(),
            conversation=uuid.uuid4(),
            thread=uuid.uuid4(),
            documents=[uuid.uuid4(), uuid.uuid4()],
            legacy=legacy,
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _seed(factory, ids)
        transport = httpx.ASGITransport(app=_app(factory, ids))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await _journey(client, factory, ids, monkeypatch)
            await _publication_survives_renewal(client, factory, ids, monkeypatch)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin.dispose()
        monkeypatch.undo()
        time.tzset()
