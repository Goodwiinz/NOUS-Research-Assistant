"""Durable evidence of remote writes whose outcomes are not yet recorded.

Callers change metadata only on a freshly loaded, locked Document. Tokens are
per call, so one writer cannot acknowledge another writer. Unknown outcomes
survive worker loss; cleanup may repeat idempotently until the writer settles.
"""

from __future__ import annotations

from typing import Any, Protocol

PENDING_WRITES = "pending_satellite_writes"


class _MetadataDocument(Protocol):
    # Legacy SQLAlchemy descriptors are not typed as their instance values.
    document_metadata: Any


def pending_writes(document: _MetadataDocument, satellite: str) -> dict[str, Any]:
    writes = (document.document_metadata or {}).get(PENDING_WRITES)
    selected = writes.get(satellite) if isinstance(writes, dict) else None
    return selected if isinstance(selected, dict) else {}


def begin_write(document: _MetadataDocument, satellite: str, token: str) -> None:
    metadata = dict(document.document_metadata or {})
    previous = metadata.get(PENDING_WRITES)
    writes = dict(previous) if isinstance(previous, dict) else {}
    writes[satellite] = {**pending_writes(document, satellite), token: {}}
    document.document_metadata = {**metadata, PENDING_WRITES: writes}


def finish_write(document: _MetadataDocument, satellite: str, token: str) -> None:
    metadata = dict(document.document_metadata or {})
    previous = metadata.get(PENDING_WRITES)
    writes = dict(previous) if isinstance(previous, dict) else {}
    satellite_writes = dict(pending_writes(document, satellite))
    satellite_writes.pop(token, None)
    if satellite_writes:
        writes[satellite] = satellite_writes
    else:
        writes.pop(satellite, None)
    if writes:
        metadata[PENDING_WRITES] = writes
    else:
        metadata.pop(PENDING_WRITES, None)
    document.document_metadata = metadata
