"""Versioned chat handoff record: harness-written structured data, never an
LLM summary. Optimistic concurrency per thread; the service owns the commit.

Every query is scoped by organization, thread and project: a chain belongs to
one (thread, project) pair, so a chat re-attached to another project starts a
fresh chain and never shows the previous project's handoffs. A conflict returns the latest version; the rejected
content is never stored.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.artifact import Artifact, ArtifactVersion
from src.models.integration_handoff import IntegrationHandoff
from src.models.thread import Thread
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_handoff import (
    HandoffConflict,
    HandoffCreate,
    HandoffDTO,
    HandoffInvalid,
)
from src.services.integrations.context import IntegrationAccessDenied, validate_binding

__all__ = [
    "HandoffConflict",
    "HandoffInvalid",
    "read_latest",
    "read_latest_for_thread",
    "save",
]


def _bound_thread(context: IntegrationContext) -> UUID:
    if context.thread_id is None:
        raise IntegrationAccessDenied()
    return context.thread_id


def _dto(row: IntegrationHandoff) -> HandoffDTO:
    return HandoffDTO.model_validate(row)


async def _latest_row(
    db: AsyncSession,
    *,
    organization_id: UUID,
    thread_id: UUID,
    project_id: UUID,
) -> IntegrationHandoff | None:
    row = await db.scalar(
        select(IntegrationHandoff)
        .where(
            IntegrationHandoff.organization_id == organization_id,
            IntegrationHandoff.thread_id == thread_id,
            IntegrationHandoff.project_id == project_id,
            IntegrationHandoff.is_deleted.is_(False),
        )
        .order_by(IntegrationHandoff.version.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return row


async def _replayed(
    db: AsyncSession, context: IntegrationContext, thread_id: UUID, handoff_id: UUID
) -> IntegrationHandoff | None:
    row = await db.scalar(
        select(IntegrationHandoff)
        .where(
            IntegrationHandoff.organization_id == context.organization_id,
            IntegrationHandoff.project_id == context.project_id,
            IntegrationHandoff.thread_id == thread_id,
            IntegrationHandoff.handoff_id == handoff_id,
            IntegrationHandoff.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    return row


def _results(payload: HandoffCreate) -> list[dict[str, str]]:
    return [
        {"artifact_version_id": str(r.artifact_version_id), "summary": r.summary}
        for r in payload.results
    ]


def _body(payload: HandoffCreate) -> dict[str, Any]:
    return {
        "version": (payload.expected_parent_version or 0) + 1,
        "goal": payload.goal,
        "decisions": list(payload.decisions),
        "remaining": list(payload.remaining),
        "results": _results(payload),
        "harness_name": payload.harness_name,
        "harness_session_id": payload.harness_session_id,
    }


def _same_body(row: IntegrationHandoff, payload: HandoffCreate) -> bool:
    return all(getattr(row, key) == value for key, value in _body(payload).items())


async def _check_results(
    db: AsyncSession, context: IntegrationContext, payload: HandoffCreate
) -> None:
    wanted = {r.artifact_version_id for r in payload.results}
    if not wanted:
        return
    found = set(
        (
            await db.scalars(
                select(ArtifactVersion.id)
                .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
                .where(
                    ArtifactVersion.id.in_(wanted),
                    ArtifactVersion.is_deleted.is_(False),
                    Artifact.is_deleted.is_(False),
                    Artifact.project_id == context.project_id,
                    Artifact.organization_id == context.organization_id,
                )
            )
        ).all()
    )
    if found != wanted:
        raise HandoffInvalid(
            "Result references an artifact version outside the project"
        )


_RACE_CONSTRAINTS = "uq_integration_handoffs_"
# SQLite (unit tests) reports columns, not the constraint name.
_SQLITE_UNIQUE = "UNIQUE constraint failed: integration_handoffs."


def _is_chain_race(error: IntegrityError) -> bool:
    """Only our per-chain uniqueness means "lost a race"; any other integrity
    failure (e.g. a foreign key) is a real error and must surface."""
    message = str(error.orig)
    return _RACE_CONSTRAINTS in message or _SQLITE_UNIQUE in message


async def _conflict(
    db: AsyncSession, context: IntegrationContext, thread_id: UUID
) -> HandoffConflict:
    latest = await _latest_row(
        db,
        organization_id=context.organization_id,
        thread_id=thread_id,
        project_id=context.project_id,
    )
    return HandoffConflict(_dto(latest) if latest is not None else None)


async def read_latest(
    db: AsyncSession, context: IntegrationContext
) -> HandoffDTO | None:
    thread_id = _bound_thread(context)
    row = await _latest_row(
        db,
        organization_id=context.organization_id,
        thread_id=thread_id,
        project_id=context.project_id,
    )
    return _dto(row) if row is not None else None


async def read_latest_for_thread(
    db: AsyncSession, *, organization_id: UUID, thread_id: UUID
) -> HandoffDTO | None:
    """Browser path; the caller has already authorized the thread. Only the
    chain of the thread's current project is shown."""
    row = await db.scalar(
        select(IntegrationHandoff)
        .join(Thread, Thread.id == IntegrationHandoff.thread_id)
        .where(
            IntegrationHandoff.organization_id == organization_id,
            IntegrationHandoff.thread_id == thread_id,
            IntegrationHandoff.project_id == Thread.source_project_id,
            IntegrationHandoff.is_deleted.is_(False),
        )
        .order_by(IntegrationHandoff.version.desc())
        .limit(1)
    )
    return _dto(row) if row is not None else None


async def save(
    db: AsyncSession, context: IntegrationContext, payload: HandoffCreate
) -> HandoffDTO:
    thread_id = _bound_thread(context)
    # The thread must still belong to the grant's project, owner and org.
    await validate_binding(
        db,
        user_id=context.user_id,
        organization_id=context.organization_id,
        project_id=context.project_id,
        thread_id=thread_id,
    )
    replay = await _replayed(db, context, thread_id, payload.handoff_id)
    if replay is not None:
        if _same_body(replay, payload):
            return _dto(replay)
        raise await _conflict(db, context, thread_id)
    latest = await _latest_row(
        db,
        organization_id=context.organization_id,
        thread_id=thread_id,
        project_id=context.project_id,
    )
    if payload.expected_parent_version != (latest.version if latest else None):
        raise await _conflict(db, context, thread_id)
    await _check_results(db, context, payload)
    row = IntegrationHandoff(
        id=uuid4(),
        organization_id=context.organization_id,
        project_id=context.project_id,
        thread_id=thread_id,
        handoff_id=payload.handoff_id,
        grant_id=context.grant_id,
        consent_id=context.consent_id,
        created_by_user_id=context.user_id,
        **_body(payload),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        if not _is_chain_race(error):
            raise
        # A concurrent writer took this version (or this handoff_id) first.
        replay = await _replayed(db, context, thread_id, payload.handoff_id)
        if replay is not None and _same_body(replay, payload):
            return _dto(replay)
        raise await _conflict(db, context, thread_id)
    return _dto(row)
