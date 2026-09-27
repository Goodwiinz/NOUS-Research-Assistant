"""Single source of truth for linking a chat thread to a project.

Background: a thread→project link lives in TWO places — the scalar
``thread.source_project_id`` (+ ``rag_document_scope``) and a ``project_threads``
join row. Three call sites historically wrote these independently with
divergent ``link_type``s, and the create-thread path swallowed errors, so the
two could desync (row but no column, or neither). The agent then forgot the
attached project. This helper writes BOTH in the caller's transaction so they
land together or not at all, and is idempotent so re-linking repairs a desync
instead of erroring.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import CollectionDocument, ProjectThread


async def get_project_document_scope(project_id: UUID, db: AsyncSession) -> list[str]:
    """Return non-deleted document IDs linked to a project (RAG scope)."""
    from src.models.document import Document
    from src.services.research_engine.project_access import project_documents_query

    rows = await db.execute(
        project_documents_query(project_id).with_only_columns(Document.id)
    )
    return [str(r[0]) for r in rows.all()]


async def attach_thread_to_project(
    db: AsyncSession,
    thread,
    project_id: UUID,
    *,
    link_type: str,
    linked_by_id: Optional[UUID] = None,
    context_note: Optional[str] = None,
) -> ProjectThread:
    """Link ``thread`` to ``project_id`` atomically (caller commits).

    Sets ``thread.source_project_id`` + ``rag_document_scope`` AND upserts the
    ``ProjectThread`` join row in one unit of work. Idempotent: if a link for
    ``(project_id, thread.id)`` already exists it is reused (and the scalar
    column is still (re)written, repairing a prior desync). Does NOT commit —
    the caller owns the transaction so both writes succeed or roll back together.
    """
    thread.source_project_id = project_id
    thread.rag_document_scope = {
        **(thread.rag_document_scope or {}),
        "document_ids": await get_project_document_scope(project_id, db),
    }

    # Upsert with conflict-do-nothing so concurrent attaches are idempotent.
    # The unique constraint uq_project_thread prevents duplicates; the
    # re-select below returns the live row (restoring it if soft-deleted).
    await db.execute(
        pg_insert(ProjectThread)
        .values(
            project_id=project_id,
            thread_id=thread.id,
            link_type=link_type,
            linked_by_id=linked_by_id,
            context_note=context_note,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "thread_id"],
        )
    )

    existing = (
        await db.execute(
            select(ProjectThread).where(
                and_(
                    ProjectThread.project_id == project_id,
                    ProjectThread.thread_id == thread.id,
                )
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.is_deleted:
            existing.restore()
        return existing

    # Should never reach here: on_conflict_do_nothing ensures the row
    # exists, and the re-select above should have found it.
    raise RuntimeError(
        f"ProjectThread row missing after upsert for project={project_id} thread={thread.id}"
    )
