"""Artifact lifecycle workers: announce versions to open runs, sweep reservations.

The run ledger closes at its terminal event, but uploads and later edits can
land afterwards. The outbox row is the durable record; the ledger event is
only an announcement, suppressed when the run is closed or unbound.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.artifact import ArtifactLifecycleOutbox, ArtifactUpload
from src.services.agent.run_event_store import (
    RunAlreadyTerminalError,
    append_event,
    has_terminal_event,
)
from src.services.agent.run_event_types import RunEventType
from src.services.artifacts.storage import get_artifact_storage

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


STALE_CLAIM = timedelta(minutes=5)


async def drain_artifact_outbox(db: AsyncSession, *, limit: int = 100) -> int:
    """Deliver pending announcements. Returns how many events were appended.

    Each row is claimed with a conditional UPDATE and committed before any
    delivery work, so a second worker that selects the same batch after our
    per-row commit released the FOR UPDATE lock cannot deliver it twice. A
    claim older than STALE_CLAIM (worker died mid-row) becomes eligible again.
    """
    now = _now()
    candidates = (
        await db.scalars(
            select(ArtifactLifecycleOutbox.id)
            .where(
                or_(
                    ArtifactLifecycleOutbox.status == "pending",
                    and_(
                        ArtifactLifecycleOutbox.status == "processing",
                        ArtifactLifecycleOutbox.updated_at < now - STALE_CLAIM,
                    ),
                )
            )
            .order_by(ArtifactLifecycleOutbox.created_at)
            .limit(limit)
        )
    ).all()
    delivered = 0
    for row_id in candidates:
        claimed = await db.execute(
            update(ArtifactLifecycleOutbox)
            .where(
                ArtifactLifecycleOutbox.id == row_id,
                or_(
                    ArtifactLifecycleOutbox.status == "pending",
                    and_(
                        ArtifactLifecycleOutbox.status == "processing",
                        ArtifactLifecycleOutbox.updated_at < now - STALE_CLAIM,
                    ),
                ),
            )
            .values(
                status="processing",
                attempts=ArtifactLifecycleOutbox.attempts + 1,
                updated_at=now,
            )
        )
        await db.commit()
        if cast(CursorResult[Any], claimed).rowcount != 1:
            continue  # another worker claimed it
        row = await db.get(ArtifactLifecycleOutbox, row_id, populate_existing=True)
        if row is None:
            continue
        if row.run_id is None or await has_terminal_event(db, row.run_id):
            # Closed ledger: suppress only the announcement; the row stays queryable.
            row.status = "skipped"
        else:
            try:
                await append_event(
                    db,
                    run_id=row.run_id,
                    event_type=RunEventType.ARTIFACT_VERSION_CREATED,
                    payload={
                        "artifact_id": str(row.artifact_id),
                        "version_id": str(row.version_id),
                    },
                    organization_id=row.organization_id,
                )
                row.status = "delivered"
                row.delivered_at = _now()
                row.last_error = None
                delivered += 1
            except RunAlreadyTerminalError:
                row.status = "skipped"
            except Exception as error:  # noqa: BLE001 - keep draining other rows
                logger.warning("artifact announcement failed", exc_info=error)
                await db.rollback()
                row = await db.get(
                    ArtifactLifecycleOutbox, row_id, populate_existing=True
                )
                if row is None:
                    continue
                row.last_error = str(error)[:200]
                # Bounded retry: back to pending until MAX_ATTEMPTS, then skipped.
                row.status = "skipped" if row.attempts >= MAX_ATTEMPTS else "pending"
        await db.commit()
    return delivered


async def sweep_artifact_uploads(db: AsyncSession, *, limit: int = 100) -> int:
    """Soft-delete expired, unfinalized reservations, then drop their bytes.

    The row is claimed and committed first so a concurrent finalize can no
    longer succeed against it; only then are the bytes removed. A crash in
    between leaves an orphaned blob, never a version pointing at nothing.
    """
    now = _now()
    rows = (
        await db.scalars(
            select(ArtifactUpload)
            .where(
                ArtifactUpload.version_id.is_(None),
                ArtifactUpload.is_deleted.is_(False),
                ArtifactUpload.expires_at < now,
            )
            .order_by(ArtifactUpload.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    storage = get_artifact_storage()
    swept = 0
    for upload in rows:
        upload_id = upload.id
        key = cast(str | None, upload.storage_key)
        claimed = await db.execute(
            update(ArtifactUpload)
            .where(
                ArtifactUpload.id == upload_id,
                ArtifactUpload.version_id.is_(None),
                ArtifactUpload.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        await db.commit()
        if cast(CursorResult[Any], claimed).rowcount != 1:
            continue  # finalized or swept by someone else meanwhile
        swept += 1
        if key is not None:
            try:
                await storage.delete(key)
            except Exception as error:  # noqa: BLE001 - orphaned blob, logged
                logger.warning("artifact upload blob delete failed", exc_info=error)
                continue
            await db.execute(
                update(ArtifactUpload)
                .where(ArtifactUpload.id == upload_id)
                .values(storage_key=None)
            )
            await db.commit()
    return swept
