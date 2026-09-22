"""Shared helper functions used by tool implementations.

Contains document/project resolution, metadata sanitization, and
other utilities referenced across multiple _tool_* functions.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document
from src.models.user import User
from src.models.workspace import Workspace

logger = logging.getLogger(__name__)


# Audit R7-L11: paper ids are interpolated straight into
# ``https://arxiv.org/pdf/{id}`` (redirects followed), so anything outside the
# arXiv id grammar — new style ``2401.12345v2``, old style ``math.GT/0309136``
# — is a fetch of somewhere else entirely. Validate before it gets there.
# Lives here, not in tools.py: production dispatch reaches _tool_ingest_arxiv
# via tools_impl.execute_tool and never touches the LangChain wrapper, so both
# entry points import the check from this module.
_ARXIV_PAPER_ID_RE = re.compile(
    r"^(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$"
)


def _reject_invalid_arxiv_ids(
    paper_ids: Optional[Iterable[Any]],
) -> Optional[Dict[str, Any]]:
    """Return an error payload when any id is not a valid arXiv id (R7-L11)."""
    bad = [
        str(pid)
        for pid in (paper_ids or [])
        if not _ARXIV_PAPER_ID_RE.match(str(pid).strip())
    ]
    if bad:
        listed = ", ".join(repr(b[:64]) for b in bad[:5])
        return {
            "error": (
                f"Invalid arXiv paper id(s): {listed}. Expected forms like "
                "'2401.12345', '2401.12345v2' or 'math.GT/0309136'."
            )
        }
    return None


def _escape_like(value: str) -> str:
    """Escape special LIKE pattern characters for safe ilike() queries."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _log_resource_access_denied(
    resource_type: str, resource_id: Any, current_user: Any
) -> None:
    """Structured audit log when an agent tool can't resolve/own a resource.

    Emitted at the org/ownership-scoped query miss, so it fires for both
    not-found and cross-tenant access-denied (the query can't always tell them
    apart). Either way it's an agent access attempt worth an auditable record —
    in particular a spike of these for one user/org is the cross-tenant signal.
    Logged at info: the tool handles the miss gracefully, this is a trail not an
    alert.
    """
    try:
        logger.info(
            "agent_resource_access_denied",
            extra={
                "user_id": str(getattr(current_user, "id", "") or ""),
                "org_id": str(getattr(current_user, "organization_id", "") or ""),
                "resource_type": resource_type,
                "resource_id": str(resource_id)[:100] if resource_id else "",
            },
        )
    except Exception:
        pass


def _sanitize_metadata(metadata: Any) -> dict:
    """Coerce datetimes in a metadata dict to ISO strings for JSON serialization.

    NOT a PII/secret scrubber despite the name — it only makes the dict
    JSON-safe. Do not rely on it to redact sensitive values; use
    ``redact_pii`` / ``_scrub_tool_args`` for that.
    """
    if not isinstance(metadata, dict):
        return {}
    sanitized = {}
    for k, v in metadata.items():
        if isinstance(v, datetime):
            sanitized[k] = v.isoformat()
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_metadata(v)
        elif isinstance(v, list):
            sanitized[k] = [
                item.isoformat() if isinstance(item, datetime) else item for item in v
            ]
        else:
            sanitized[k] = v
    return sanitized


_ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")


async def _resolve_document_id(
    document_id: str,
    db: AsyncSession,
    current_user: User,
) -> Optional[Document]:
    """Resolve a document_id string (UUID, arXiv ID, or title) to a Document.

    Resolution order:
      1. UUID parse → match Document.id
      2. arXiv ID pattern (e.g. ``2303.15563`` or ``2303.15563v1``) →
         match Document.filename containing the bare ID OR
         document_metadata->>'arxiv_id' equals the bare ID.
      3. Title ILIKE fallback.

    Returns None if not found. Trace 019e1569 showed the agent passing the
    raw arXiv ID where the tool expected an internal UUID — without this
    branch the user got "Document not found or access denied".
    """
    # Try as UUID first
    try:
        doc_uuid = UUID(document_id)
        stmt = select(Document).where(
            Document.id == doc_uuid,
            Document.organization_id == current_user.organization_id,
            Document.is_deleted == False,
        )
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()
        if doc:
            return doc
    except (ValueError, AttributeError):
        pass

    # Try arXiv ID pattern (strip version suffix for the lookup)
    arxiv_match = _ARXIV_ID_RE.match(document_id.strip()) if document_id else None
    if arxiv_match:
        bare_id = arxiv_match.group(1)
        try:
            stmt = (
                select(Document)
                .where(
                    Document.organization_id == current_user.organization_id,
                    Document.is_deleted == False,
                    (
                        Document.filename.ilike(f"%{_escape_like(bare_id)}%")
                        # .as_string(), NOT .astext — generic sqlalchemy.JSON
                        # has no astext; it raised at statement-build time and
                        # the except below silently killed BOTH lookups.
                        | (
                            Document.document_metadata["arxiv_id"].as_string()
                            == bare_id
                        )
                    ),
                )
                .order_by(desc(Document.created_at))
                .limit(1)
            )
            result = await db.execute(stmt)
            doc = result.scalar_one_or_none()
            if doc:
                return doc
        except Exception:
            logger.debug("arxiv_id resolution failed for %r", bare_id, exc_info=True)

    # Try by title (case-insensitive).
    #
    # Audit R7-L8: this took the newest of any substring match, so "report"
    # silently resolved to whichever "Q3 Report (draft)" happened to be newest
    # and add_document_to_project / summarize_document / extract_entities then
    # acted on the wrong document. Match on exact case-insensitive title
    # equality instead: a substring query also returns "Annual Report" for
    # "Report", and the "exactly one row" rule then rejected the unambiguous
    # exact match. limit(2) is enough to spot a duplicate title — two rows is
    # ambiguous, so it's a miss and callers handle that.
    if document_id:
        try:
            wanted = document_id.strip().lower()
            stmt = (
                select(Document)
                .where(
                    func.lower(Document.title) == wanted,
                    Document.organization_id == current_user.organization_id,
                    Document.is_deleted == False,
                )
                .order_by(desc(Document.created_at))
                .limit(2)
            )
            result = await db.execute(stmt)
            docs = list(result.scalars().all())
            if len(docs) == 1:
                return docs[0]
        except Exception:
            pass

    _log_resource_access_denied("document", document_id, current_user)
    return None


async def _resolve_project_id(
    project_id: str,
    db: AsyncSession,
    current_user: User,
) -> Optional[UUID]:
    """Resolve a project_id string to a UUID.

    Accepts either a UUID string or a project name. Returns None if not found.
    """
    # Try as UUID first
    try:
        return UUID(project_id)
    except (ValueError, AttributeError):
        pass

    # Try by name (case-insensitive)
    if project_id and db and current_user:
        try:
            stmt = (
                select(Collection.id)
                .join(Workspace, Collection.workspace_id == Workspace.id)
                .where(
                    Collection.name.ilike(_escape_like(project_id)),
                    Collection.is_deleted == False,
                    Workspace.owner_id == current_user.id,
                )
                .limit(1)
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                return row
        except Exception:
            pass

    return None


async def _verify_project_ownership(
    project_id: str,
    db: AsyncSession,
    current_user: User,
) -> Optional[Collection]:
    """Verify a project (collection) exists and belongs to the current user."""
    proj_uuid = await _resolve_project_id(project_id, db, current_user)
    if not proj_uuid:
        return None

    stmt = (
        select(Collection)
        .join(Workspace, Collection.workspace_id == Workspace.id)
        .where(
            Collection.id == proj_uuid,
            Collection.is_deleted == False,
            Workspace.owner_id == current_user.id,
        )
    )
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        _log_resource_access_denied("project", project_id, current_user)
    return project


async def _link_documents_to_project(
    db: AsyncSession,
    project: Collection,
    document_ids: Iterable[str],
) -> Dict[str, Any]:
    """Idempotently link documents to a project.

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` against the
    ``(collection_id, document_id)`` unique constraint so concurrent
    ingests of the same paper don't race the existence check, and so we
    avoid an N+1 ``SELECT`` per document.

    Returns ``{"linked": int, "already_linked": int}`` — caller decides
    how to surface the result.
    """
    ids = [str(d) for d in document_ids if d]
    if not ids:
        return {"linked": 0, "already_linked": 0}

    existing_stmt = select(CollectionDocument.document_id).where(
        CollectionDocument.collection_id == project.id,
        CollectionDocument.document_id.in_(ids),
    )
    existing = {str(row[0]) for row in (await db.execute(existing_stmt)).all()}

    new_rows = [
        {"collection_id": project.id, "document_id": doc_id}
        for doc_id in ids
        if doc_id not in existing
    ]

    if new_rows:
        await db.execute(
            pg_insert(CollectionDocument)
            .values(new_rows)
            .on_conflict_do_nothing(index_elements=["collection_id", "document_id"])
        )

    return {
        "linked": len(new_rows),
        "already_linked": len(existing),
    }
