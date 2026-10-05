"""Durable evidence of remote writes whose outcomes are not yet recorded.

Callers change metadata only on a freshly loaded, locked Document. Tokens are
per call, so one writer cannot acknowledge another writer. Unknown outcomes
survive worker loss; cleanup may repeat idempotently until the writer settles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

PENDING_WRITES = "pending_satellite_writes"

# A writer that started longer ago than this is dead: Celery's hard
# ``task_time_limit`` (600s, celery_app.py) has killed it, so whatever it sent
# the provider has landed or never will. The same bound as
# ``replay_guard.DEFAULT_STALE_RUNNING_SECONDS``.
WRITER_LEASE = timedelta(seconds=900)


class _MetadataDocument(Protocol):
    # Legacy SQLAlchemy descriptors are not typed as their instance values.
    document_metadata: Any


def pending_writes(document: _MetadataDocument, satellite: str) -> dict[str, Any]:
    writes = (document.document_metadata or {}).get(PENDING_WRITES)
    selected = writes.get(satellite) if isinstance(writes, dict) else None
    return selected if isinstance(selected, dict) else {}


def _started_before(entry: Any, cutoff: datetime) -> bool:
    started = entry.get("started_at") if isinstance(entry, dict) else None
    if not isinstance(started, str):
        return True  # no recorded start (legacy ``{}`` token): treat as dead
    try:
        return datetime.fromisoformat(started) <= cutoff
    except ValueError:
        return True


def _store(document: _MetadataDocument, satellite: str, tokens: dict) -> None:
    metadata = dict(document.document_metadata or {})
    previous = metadata.get(PENDING_WRITES)
    writes = dict(previous) if isinstance(previous, dict) else {}
    if tokens:
        writes[satellite] = tokens
    else:
        writes.pop(satellite, None)
    if writes:
        metadata[PENDING_WRITES] = writes
    else:
        metadata.pop(PENDING_WRITES, None)
    document.document_metadata = metadata


def begin_write(
    document: _MetadataDocument,
    satellite: str,
    token: str,
    *,
    now: datetime | None = None,
) -> None:
    """Record ``token`` as an in-flight write; drop this satellite's tokens
    whose writers are past the lease. On a live document their outcome no
    longer matters (a later sync reuses any data source they created), and
    pruning here bounds growth when every sync fails."""
    now = now or datetime.now(timezone.utc)
    live = {
        t: e
        for t, e in pending_writes(document, satellite).items()
        if not _started_before(e, now - WRITER_LEASE)
    }
    live[token] = {"started_at": now.isoformat()}
    _store(document, satellite, live)


def finish_write(document: _MetadataDocument, satellite: str, token: str) -> None:
    tokens = dict(pending_writes(document, satellite))
    tokens.pop(token, None)
    _store(document, satellite, tokens)


def retire_settled_writes(
    document: _MetadataDocument, satellite: str, cleanup_started: datetime
) -> None:
    """After a cleanup that began at ``cleanup_started`` succeeded, drop tokens
    whose writers were already dead by then: anything they sent had landed
    before the cleanup ran, so it was removed. Younger tokens stay until a
    later cleanup outlives them."""
    cutoff = cleanup_started - WRITER_LEASE
    _store(
        document,
        satellite,
        {
            t: e
            for t, e in pending_writes(document, satellite).items()
            if not _started_before(e, cutoff)
        },
    )
