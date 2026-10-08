"""Real-PostgreSQL proof that the soft-deleted-thread purge can delete a chat
that harness and artifact rows still reference (audit HO-2).

``purge_soft_deleted_threads`` deletes chat_messages and then the thread with
bulk DELETEs, so only the database's ON DELETE rules clear referencing rows.
SQLite does not enforce foreign keys, so ``tests/unit/tasks/test_retention_tasks.py``
never saw that seven FKs added 09-28..10-05 (and
``artifact_references.message_id``) had none: the first such chat rolled back
every organization's batch on every run.

The tables come from the models (``Base.metadata.create_all``), so the purge
tests prove the models' rules; ``test_migration_*`` runs the rp01 revision
itself against the same tables and checks it lands the same rules.

Requires ``RETENTION_PURGE_TEST_DATABASE_URL`` (CI) or the local
``ORCHESTRATION_TEST_DATABASE_URL`` fallback; skipped (NOT RUN) otherwise.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator, cast
from unittest.mock import patch

import pytest
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

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
from src.models.bridge_device import BridgeDevice
from src.models.chat_message import ChatMessage, MessageRole
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_context_selection import IntegrationContextSelection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.integration_handoff import IntegrationHandoff
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.tasks import retention_tasks

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

NOW = datetime.now(timezone.utc)

_MODELS: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    Conversation,
    Thread,
    ChatMessage,
    AgentRun,
    AgentRuntimeSnapshot,
    BridgeDevice,
    IntegrationGrantRequest,
    IntegrationGrant,
    IntegrationContextSelection,
    IntegrationHandoff,
    IntegrationToolAction,
    Artifact,
    ArtifactUpload,
    ArtifactVersion,
    ArtifactReference,
    ArtifactLifecycleOutbox,
]

# (table, column) -> pg_constraint.confdeltype: c = CASCADE, n = SET NULL.
# Must match rp01_thread_fk_ondelete.FOREIGN_KEYS and the models.
EXPECTED_RULES: dict[tuple[str, str], str] = {
    ("integration_handoffs", "thread_id"): "c",
    ("integration_grant_requests", "thread_id"): "c",
    ("integration_context_selections", "consent_id"): "c",
    ("integration_grants", "request_id"): "n",
    ("integration_handoffs", "consent_id"): "n",
    ("integration_grants", "thread_id"): "n",
    ("integration_tool_actions", "thread_id"): "n",
    ("artifact_versions", "thread_id"): "n",
    ("artifact_references", "thread_id"): "n",
    ("artifact_references", "message_id"): "n",
    ("artifact_lifecycle_outbox", "thread_id"): "n",
}


def _dsn() -> str:
    dsn = (
        os.getenv("RETENTION_PURGE_TEST_DATABASE_URL")
        or os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
        or ""
    )
    if not dsn:
        pytest.skip("Retention purge PostgreSQL test database is not configured")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg2://" + dsn[len(prefix) :]
    raise ValueError("the retention purge test database must be PostgreSQL")


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A throwaway schema holding every table a chat's purge touches."""
    dsn = _dsn()
    schema = "retention_purge_" + uuid.uuid4().hex
    admin = create_engine(dsn)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped = create_engine(dsn, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(
            scoped, tables=[cast(Any, model).__table__ for model in _MODELS]
        )
        yield scoped
    finally:
        scoped.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        RETENTION_ENABLED=True,
        RETENTION_APPLY=True,
        RETENTION_SOFT_DELETED_THREAD_DAYS=30,
        RETENTION_BATCH_SIZE=500,
    )


def _purge(engine: Engine) -> dict[str, Any]:
    factory = sessionmaker(engine, autocommit=False, autoflush=False)
    with (
        patch.object(retention_tasks, "SessionLocal", factory),
        patch.object(retention_tasks, "get_settings", return_value=_settings()),
    ):
        return cast(dict[str, Any], retention_tasks.purge_soft_deleted_threads())


def _hex64() -> str:
    return uuid.uuid4().hex * 2


def _seed_owner(db: Session) -> SimpleNamespace:
    """An organization, its user, a workspace and one project (Collection)."""
    ids = SimpleNamespace(
        org=uuid.uuid4(),
        user=uuid.uuid4(),
        workspace=uuid.uuid4(),
        project=uuid.uuid4(),
        conversation=uuid.uuid4(),
    )
    db.execute(
        text("""INSERT INTO organizations (id, created_at, updated_at,
            is_deleted, name, storage_tier, storage_used_bytes,
            storage_limit_bytes, is_active) VALUES (:id, now(), now(),
            false, 'org', 'FREE', 0, 1000000, true)"""),
        {"id": ids.org},
    )
    db.execute(
        text("""INSERT INTO users (id, created_at, updated_at, is_deleted,
            email, password_hash, first_name, last_name, role, is_active,
            organization_id, login_count) VALUES (:id, now(), now(), false,
            :email, 'unused', 'x', 'y', 'USER', true, :org, 0)"""),
        {"id": ids.user, "email": f"{ids.user}@example.test", "org": ids.org},
    )
    # One flush per parent level: most of these models declare no
    # relationship(), so the unit of work cannot order their INSERTs.
    for row in (
        Workspace(
            id=ids.workspace, name="w", owner_id=ids.user, organization_id=ids.org
        ),
        Collection(id=ids.project, workspace_id=ids.workspace, name="p"),
        Conversation(
            id=ids.conversation,
            workspace_id=ids.workspace,
            title="c",
            created_by_id=ids.user,
        ),
    ):
        db.add(row)
        db.flush()
    return ids


def _seed_thread(db: Session, owner: SimpleNamespace, *, age_days: int) -> uuid.UUID:
    """A chat soft-deleted ``age_days`` ago, with one message."""
    thread_id = uuid.uuid4()
    deleted_at = NOW - timedelta(days=age_days)
    db.add(
        Thread(
            id=thread_id,
            conversation_id=owner.conversation,
            title="t",
            is_deleted=True,
            created_at=deleted_at,
            updated_at=deleted_at,
        )
    )
    db.flush()
    db.add(ChatMessage(thread_id=thread_id, role=MessageRole.USER, content="m"))
    db.flush()
    return thread_id


def _bind_everything(
    db: Session, owner: SimpleNamespace, thread_id: uuid.UUID
) -> SimpleNamespace:
    """Every row kind that names the chat: a chat-bound consent and its memory
    selection, the device grant minted from it, a handoff, a tool action, and
    a published artifact version with its reference and outbox row."""
    ids = SimpleNamespace(
        device=uuid.uuid4(),
        consent=uuid.uuid4(),
        project_consent=uuid.uuid4(),
        grant=uuid.uuid4(),
        action=uuid.uuid4(),
        artifact=uuid.uuid4(),
        upload=uuid.uuid4(),
        version=uuid.uuid4(),
        reference=uuid.uuid4(),
        outbox=uuid.uuid4(),
    )
    message_id = db.scalar(
        select(ChatMessage.id).where(ChatMessage.thread_id == thread_id)
    )
    expired = NOW - timedelta(days=40)
    db.add(
        BridgeDevice(
            id=ids.device, user_id=owner.user, organization_id=owner.org, label="d"
        )
    )
    db.flush()
    for consent_id, bound_thread in (
        (ids.consent, thread_id),
        # The same device's project-wide consent: no chat, so the purge of
        # this chat must leave it alone.
        (ids.project_consent, None),
    ):
        db.add(
            IntegrationGrantRequest(
                id=consent_id,
                user_id=owner.user,
                organization_id=owner.org,
                project_id=owner.project,
                device_id=ids.device,
                thread_id=bound_thread,
                scopes=["handoff:read", "handoff:write"],
                status="consumed",
                expires_at=expired,
            )
        )
    db.flush()
    db.add(
        IntegrationGrant(
            id=ids.grant,
            user_id=owner.user,
            organization_id=owner.org,
            project_id=owner.project,
            device_id=ids.device,
            request_id=ids.consent,
            thread_id=thread_id,
            scopes=["handoff:read", "handoff:write"],
            token_hash=_hex64(),
            expires_at=expired,
            revoked_at=expired,
            consented_at=expired,
        )
    )
    db.add(
        Artifact(
            id=ids.artifact,
            organization_id=owner.org,
            project_id=owner.project,
            owner_id=owner.user,
            title="a",
            current_version_id=ids.version,
        )
    )
    db.add(
        ArtifactUpload(
            id=ids.upload,
            organization_id=owner.org,
            project_id=owner.project,
            grant_id=ids.grant,
            publication_id=uuid.uuid4(),
            byte_size=1,
            mime_type="text/plain",
            sha256=_hex64(),
            request_hash=_hex64(),
            expires_at=expired,
        )
    )
    db.flush()
    # Each consent's memory selection; only the chat-bound one goes with it.
    for selected_consent in (ids.consent, ids.project_consent):
        db.add(
            IntegrationContextSelection(
                organization_id=owner.org,
                user_id=owner.user,
                project_id=owner.project,
                consent_id=selected_consent,
                memory_ids=[],
            )
        )
    db.add(
        IntegrationHandoff(
            organization_id=owner.org,
            project_id=owner.project,
            thread_id=thread_id,
            version=1,
            handoff_id=uuid.uuid4(),
            goal="g",
            decisions=[],
            remaining=[],
            results=[],
            harness_name="codex",
            grant_id=ids.grant,
            consent_id=ids.consent,
            created_by_user_id=owner.user,
        )
    )
    db.add(
        IntegrationToolAction(
            id=ids.action,
            organization_id=owner.org,
            user_id=owner.user,
            project_id=owner.project,
            thread_id=thread_id,
            grant_id=ids.grant,
            consent_id=ids.consent,
            invocation_id=uuid.uuid4(),
            tool_name="create_project_note",
            arguments={},
            argument_hash=_hex64(),
            state="succeeded",
        )
    )
    db.add(
        ArtifactVersion(
            id=ids.version,
            artifact_id=ids.artifact,
            upload_id=ids.upload,
            title="a",
            mime_type="text/plain",
            byte_size=1,
            sha256=_hex64(),
            storage_key="k",
            producer="harness",
            provenance={"producer": "harness"},
            thread_id=thread_id,
            grant_id=ids.grant,
        )
    )
    db.flush()
    db.add(
        ArtifactReference(
            id=ids.reference,
            artifact_id=ids.artifact,
            version_id=ids.version,
            thread_id=thread_id,
            message_id=message_id,
        )
    )
    db.add(
        ArtifactLifecycleOutbox(
            id=ids.outbox,
            organization_id=owner.org,
            artifact_id=ids.artifact,
            version_id=ids.version,
            kind="artifact.version.created",
            thread_id=thread_id,
            status="delivered",
        )
    )
    db.flush()
    return ids


def _count(db: Session, model: Any, *where: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _rules(engine: Engine) -> dict[tuple[str, str], list[str]]:
    """confdeltype of every one-column FK on the EXPECTED_RULES columns."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT r.relname, a.attname, c.confdeltype
            FROM pg_constraint c
            JOIN pg_class r ON r.oid = c.conrelid
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.contype = 'f'
              AND r.relnamespace = to_regnamespace(current_schema())
        """)).all()
    found: dict[tuple[str, str], list[str]] = {}
    for table, column, rule in rows:
        if (table, column) in EXPECTED_RULES:
            found.setdefault((table, column), []).append(rule)
    return found


def test_purge_deletes_a_chat_that_harness_and_artifact_rows_reference(
    engine: Engine,
) -> None:
    factory = sessionmaker(engine)
    with factory() as db:
        owner = _seed_owner(db)
        # Oldest first: a clean chat ahead of the bound one in the same batch,
        # which the old purge rolled back with it.
        clean = _seed_thread(db, owner, age_days=60)
        bound = _seed_thread(db, owner, age_days=40)
        ids = _bind_everything(db, owner, bound)
        db.commit()

    result = _purge(engine)

    assert (result["threads"], result["messages"]) == (2, 2)
    with factory() as db:
        assert _count(db, Thread, Thread.id.in_([clean, bound])) == 0
        assert _count(db, ChatMessage) == 0
        # CASCADE: rows that mean nothing without the chat.
        assert _count(db, IntegrationHandoff) == 0
        assert db.get(IntegrationGrantRequest, ids.consent) is None
        # The chat-bound consent is deleted, never left with a NULL chat, which
        # mint_integration_grant reads as a project-wide consent. The cascade
        # stops there: the project-wide consent keeps its memory selection.
        consents = db.scalars(select(IntegrationGrantRequest)).all()
        assert [(c.id, c.thread_id) for c in consents] == [(ids.project_consent, None)]
        selected = db.scalars(select(IntegrationContextSelection.consent_id)).all()
        assert selected == [ids.project_consent]
        # SET NULL: the grant keeps its revocation evidence and drops both links.
        grant = db.get(IntegrationGrant, ids.grant)
        assert grant is not None and grant.revoked_at is not None
        assert (grant.thread_id, grant.request_id) == (None, None)
        action = db.get(IntegrationToolAction, ids.action)
        assert action is not None and action.thread_id is None
        # Project content survives the chat.
        version = db.get(ArtifactVersion, ids.version)
        assert version is not None and version.thread_id is None
        reference = db.get(ArtifactReference, ids.reference)
        assert reference is not None
        assert (reference.thread_id, reference.message_id) == (None, None)
        outbox = db.get(ArtifactLifecycleOutbox, ids.outbox)
        assert outbox is not None and outbox.thread_id is None


def test_models_declare_the_rules_the_migration_sets(engine: Engine) -> None:
    assert _rules(engine) == {key: [rule] for key, rule in EXPECTED_RULES.items()}
