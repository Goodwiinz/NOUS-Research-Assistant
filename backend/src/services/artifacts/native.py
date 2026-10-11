"""Durable publication of completed, server-owned drafts."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.artifact import ArtifactUpload
from src.models.generated_draft import GeneratedDraft
from src.models.native_artifact_intent import NativeArtifactIntent
from src.models.user import User
from src.schemas.artifact import (
    ArtifactAccessDenied,
    ArtifactError,
    ArtifactNotFound,
    ArtifactProvenance,
    ArtifactVersionDTO,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.service import (
    MAX_ARTIFACT_BYTES,
    _authorize_context,
    publish_version,
    reserve_upload,
    store_upload,
)
from src.services.integrations.context import IntegrationAccessDenied, validate_binding

MAX_ATTEMPTS = 5
STALE_CLAIM = timedelta(minutes=5)


class NativeDraftUnavailable(ArtifactNotFound):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__()


def create_native_context(
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID,
    thread_id: UUID | None = None,
    run_id: UUID | None = None,
) -> IntegrationContext:
    """Use server-observed identity, never tool/body arguments."""
    return IntegrationContext(
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        grant_id=uuid5(
            NAMESPACE_URL,
            f"nous:native-artifact:{user_id}:{organization_id}:{project_id}:{thread_id}:{run_id}",
        ),
    )


async def _authorize_native(db: AsyncSession, context: IntegrationContext) -> None:
    if context.project_id is None or context.consent_id is not None:
        raise ArtifactAccessDenied()
    expected = create_native_context(
        user_id=context.user_id,
        organization_id=context.organization_id,
        project_id=context.project_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
    )
    if context.grant_id != expected.grant_id:
        raise ArtifactAccessDenied()
    actor = await db.scalar(
        select(User.id).where(
            User.id == context.user_id,
            User.organization_id == context.organization_id,
            User.is_active.is_(True),
            User.is_deleted.is_(False),
        )
    )
    if actor is None:
        raise ArtifactAccessDenied()
    try:
        await validate_binding(
            db,
            user_id=context.user_id,
            organization_id=context.organization_id,
            project_id=context.project_id,
            thread_id=context.thread_id,
            run_id=context.run_id,
        )
    except IntegrationAccessDenied as error:
        raise ArtifactAccessDenied() from error
    await _authorize_context(db, context, edit=True)


def _digest(content: str) -> tuple[bytes, str]:
    data = content.encode("utf-8")
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactAccessDenied()
    return data, hashlib.sha256(data).hexdigest()


async def enqueue_native_artifact(
    db: AsyncSession,
    *,
    draft_id: UUID,
    context: IntegrationContext,
) -> None:
    """Participates in caller's draft transaction; never commits."""
    if not settings.ARTIFACTS_ENABLED:
        return
    await _authorize_native(db, context)
    draft = await db.scalar(
        select(GeneratedDraft)
        .where(
            GeneratedDraft.id == draft_id,
            GeneratedDraft.project_id == context.project_id,
            GeneratedDraft.is_deleted.is_(False),
        )
        .with_for_update()
    )
    if draft is None:
        raise ArtifactNotFound()
    existing = await db.scalar(
        select(NativeArtifactIntent.id).where(
            NativeArtifactIntent.draft_id == draft_id,
        )
    )
    if existing is not None:
        return
    _, digest = _digest(str(draft.content))
    db.add(
        NativeArtifactIntent(
            draft_id=draft_id,
            organization_id=context.organization_id,
            actor_user_id=context.user_id,
            project_id=context.project_id,
            thread_id=context.thread_id,
            run_id=str(context.run_id) if context.run_id else None,
            draft_sha256=digest,
            title=str(draft.title),
            status="pending",
            attempts=0,
        )
    )
    await db.flush()


def _eligible(now: datetime) -> Any:
    return and_(
        NativeArtifactIntent.is_deleted.is_(False),
        NativeArtifactIntent.attempts < MAX_ATTEMPTS,
        or_(
            NativeArtifactIntent.status == "pending",
            and_(
                NativeArtifactIntent.status == "processing",
                NativeArtifactIntent.claimed_at < now - STALE_CLAIM,
            ),
        ),
    )


async def _reconcile_exhausted(db: AsyncSession, now: datetime, limit: int) -> int:
    exhausted = and_(
        NativeArtifactIntent.is_deleted.is_(False),
        NativeArtifactIntent.attempts >= MAX_ATTEMPTS,
        or_(
            NativeArtifactIntent.status == "pending",
            and_(
                NativeArtifactIntent.status == "processing",
                NativeArtifactIntent.claimed_at < now - STALE_CLAIM,
            ),
        ),
    )
    candidates = (
        await db.scalars(
            select(NativeArtifactIntent.id)
            .where(exhausted)
            .order_by(NativeArtifactIntent.created_at, NativeArtifactIntent.id)
            .limit(limit)
        )
    ).all()
    delivered = 0
    for intent_id in candidates:
        row = await db.scalar(
            select(NativeArtifactIntent)
            .where(
                NativeArtifactIntent.id == intent_id,
                exhausted,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            await db.commit()
            continue
        # A final worker may have committed publication before losing its ack.
        # Reconcile that durable receipt without another storage attempt.
        context = create_native_context(
            user_id=row.actor_user_id,
            organization_id=row.organization_id,
            project_id=row.project_id,
            thread_id=row.thread_id,
            run_id=UUID(row.run_id) if row.run_id else None,
        )
        version_id = await db.scalar(
            select(ArtifactUpload.version_id).where(
                ArtifactUpload.grant_id == context.grant_id,
                ArtifactUpload.organization_id == row.organization_id,
                ArtifactUpload.project_id == row.project_id,
                ArtifactUpload.publication_id
                == uuid5(NAMESPACE_URL, f"nous:native-draft:{row.draft_id}"),
                ArtifactUpload.is_deleted.is_(False),
            )
        )
        acknowledged = await db.execute(
            update(NativeArtifactIntent)
            .where(
                NativeArtifactIntent.id == row.id,
                exhausted,
                NativeArtifactIntent.claim_token == row.claim_token,
            )
            .values(
                status="delivered" if version_id else "failed",
                version_id=version_id,
                last_error=None if version_id else "publication_attempts_exhausted",
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if (
            version_id is not None
            and cast(CursorResult[Any], acknowledged).rowcount == 1
        ):
            delivered += 1
    return delivered


async def _source_content(
    db: AsyncSession, row: NativeArtifactIntent, *, lock: bool = False
) -> tuple[bytes, str]:
    query = (
        select(GeneratedDraft)
        .where(
            GeneratedDraft.id == row.draft_id,
            GeneratedDraft.project_id == row.project_id,
            GeneratedDraft.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    draft = await db.scalar(query)
    if draft is None:
        raise NativeDraftUnavailable("draft_unavailable")
    content, digest = _digest(str(draft.content))
    if digest != row.draft_sha256 or str(draft.title) != row.title:
        raise NativeDraftUnavailable("draft_changed")
    return content, digest


async def _publish_intent(
    db: AsyncSession, row: NativeArtifactIntent
) -> ArtifactVersionDTO:
    intent_id, expected_token = row.id, row.claim_token
    context = create_native_context(
        user_id=row.actor_user_id,
        organization_id=row.organization_id,
        project_id=row.project_id,
        thread_id=row.thread_id,
        run_id=UUID(row.run_id) if row.run_id else None,
    )
    await _authorize_native(db, context)
    content, digest = await _source_content(db, row)
    # Snapshot before the publication protocol commits/expires ORM state.
    title, draft_id = row.title, row.draft_id
    publication_id = uuid5(NAMESPACE_URL, f"nous:native-draft:{draft_id}")
    upload = await reserve_upload(
        db,
        context,
        ReserveArtifactUploadRequest(
            publication_id=publication_id,
            byte_size=len(content),
            mime_type="text/markdown",
            sha256=digest,
        ),
    )
    await store_upload(db, context, upload.upload_id, content)

    async def guard(session: AsyncSession) -> None:
        current_claim = await session.scalar(
            select(NativeArtifactIntent.id)
            .where(
                NativeArtifactIntent.id == intent_id,
                NativeArtifactIntent.is_deleted.is_(False),
                NativeArtifactIntent.status == "processing",
                NativeArtifactIntent.claim_token == expected_token,
            )
            .with_for_update()
        )
        if current_claim is None:
            raise NativeDraftUnavailable("claim_unavailable")
        await _authorize_native(session, context)
        await _source_content(session, row, lock=True)

    return await publish_version(
        db,
        context,
        PublishVersionRequest(
            publication_id=publication_id,
            upload_id=upload.upload_id,
            title=title,
            provenance=ArtifactProvenance(
                producer="nous", native_item_id=str(draft_id)
            ),
        ),
        publication_guard=guard,
    )


async def drain_native_artifacts(db: AsyncSession, *, limit: int = 100) -> int:
    """Fenced claim, exact publication, conditional acknowledgement."""
    if not settings.ARTIFACTS_ENABLED:
        return 0
    now = datetime.now(timezone.utc)
    limit = min(max(limit, 0), 100)
    delivered = await _reconcile_exhausted(db, now, limit)
    candidates = (
        await db.scalars(
            select(NativeArtifactIntent.id)
            .where(_eligible(now))
            .order_by(NativeArtifactIntent.created_at, NativeArtifactIntent.id)
            .limit(limit)
        )
    ).all()
    for intent_id in candidates:
        token = uuid4()
        claimed = await db.execute(
            update(NativeArtifactIntent)
            .where(
                NativeArtifactIntent.id == intent_id,
                _eligible(now),
            )
            .values(
                status="processing",
                attempts=NativeArtifactIntent.attempts + 1,
                claim_token=token,
                claimed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if cast(CursorResult[Any], claimed).rowcount != 1:
            continue
        row = await db.get(NativeArtifactIntent, intent_id, populate_existing=True)
        if row is None:
            continue
        if row.status != "processing" or row.claim_token != token:
            # The lease may have been replaced while this worker was paused
            # before reloading. Never borrow the replacement worker's token.
            continue
        attempts = row.attempts
        outcome, version_id, error_code = "delivered", None, None
        try:
            version = await _publish_intent(db, row)
            version_id = version.version_id
        except NativeDraftUnavailable as error:
            outcome, error_code = "skipped", error.code
            await db.rollback()
        except (ArtifactAccessDenied, ArtifactNotFound):
            outcome, error_code = "skipped", "access_unavailable"
            await db.rollback()
        except (ArtifactError, OSError):
            outcome = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            error_code = "publication_unavailable"
            await db.rollback()
        except Exception:
            # No source bytes or raw storage/provider errors enter status.
            outcome = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            error_code = "publication_failed"
            await db.rollback()
        acknowledged = await db.execute(
            update(NativeArtifactIntent)
            .where(
                NativeArtifactIntent.id == intent_id,
                NativeArtifactIntent.status == "processing",
                NativeArtifactIntent.claim_token == token,
            )
            .values(status=outcome, version_id=version_id, last_error=error_code)
        )
        await db.commit()
        if (
            outcome == "delivered"
            and cast(CursorResult[Any], acknowledged).rowcount == 1
        ):
            delivered += 1
    return delivered
