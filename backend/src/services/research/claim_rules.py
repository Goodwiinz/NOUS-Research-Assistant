"""Pure claim rules (GOO-306): no database, no FastAPI.

A claim version's text is exactly ``draft.content[start_char:end_char]``
(code points, as GOO-305 offsets are). Link shapes mirror the database
CHECKs so a bad request is a 422 before any insert. Callers turn
``ValueError`` into a 422.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence
from uuid import UUID

from src.models.evidence import StanceEnum
from src.services.evidence.consensus_calculator import ConsensusCalculator

KINDS = ("factual", "interpretation")
LINK_KINDS = ("extraction", "source_span", "legacy_unanchored")
LINK_STATUSES = ("linked", "withdrawn")
STANCES = tuple(s.value for s in StanceEnum) + ("unresolved",)
# Stances an adjudicator may record without citing a link.
UNCITED_STANCES = frozenset({"unresolved", "not_addressed"})
TEXT_MISMATCH = "Text does not match the draft passage"
_SPAN = ("start_char", "end_char", "quote")
_calculator = ConsensusCalculator()


def content_hash(content: str) -> str:
    """sha256 of the UTF-8 draft content (the DraftReview recipe)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalized_hash(text: str) -> str:
    """The stance meter's claim key (``stance_classifications.claim_hash``)."""
    return _calculator._generate_claim_hash(text)


def check_passage(content: str, start: int, end: int, text: str) -> None:
    if not 0 <= start < end <= len(content):
        raise ValueError("Passage offsets are out of range")
    if not text.strip():
        raise ValueError("Claim text is blank")
    if content[start:end] != text:
        raise ValueError(TEXT_MISMATCH)


def check_link_shape(
    kind: str,
    *,
    accepted_value_id: UUID | None,
    draft_citation_id: UUID | None,
    document_id: UUID | None,
    source_hash: str | None,
    text_sha256: str | None,
    start_char: int | None,
    end_char: int | None,
    quote: str | None,
    status: str,
    supersedes_link_id: UUID | None,
) -> None:
    """Mirror of the ``research_claim_evidence_links`` CHECKs."""
    cols: dict[str, Any] = {
        "accepted_value_id": accepted_value_id,
        "draft_citation_id": draft_citation_id,
        "document_id": document_id,
        "source_hash": source_hash,
        "text_sha256": text_sha256,
        "start_char": start_char,
        "end_char": end_char,
        "quote": quote,
    }
    if status not in LINK_STATUSES:
        raise ValueError("Link status must be linked or withdrawn")
    if status == "withdrawn" and supersedes_link_id is None:
        raise ValueError("A withdrawal must supersede a link")
    if kind == "extraction":
        required: tuple[str, ...] = ("accepted_value_id", "document_id", "source_hash")
        forbidden: tuple[str, ...] = ("draft_citation_id", *_SPAN)
    elif kind == "source_span":
        required = ("document_id", "source_hash", "text_sha256", *_SPAN)
        forbidden = ("accepted_value_id", "draft_citation_id")
    elif kind == "legacy_unanchored":
        required = ("draft_citation_id",)
        forbidden = ("accepted_value_id", "source_hash", "text_sha256", *_SPAN)
    else:
        raise ValueError(f"Link kind must be one of {', '.join(LINK_KINDS)}")
    missing = [name for name in required if cols[name] is None]
    if missing:
        raise ValueError(f"A {kind} link requires {', '.join(missing)}")
    extra = [name for name in forbidden if cols[name] is not None]
    if extra:
        raise ValueError(f"A {kind} link must not carry {', '.join(extra)}")
    if kind == "source_span":
        assert start_char is not None and end_char is not None and quote is not None
        if not 0 <= start_char < end_char or not quote.strip():
            raise ValueError("A source span needs 0 <= start < end and a quote")


def check_assessment(
    stance: str,
    link_ids: Sequence[UUID],
    observation_link_ids: Sequence[UUID],
    live_link_ids: Sequence[UUID],
) -> None:
    """``observation_link_ids``: the link of each cited observation."""
    if stance not in STANCES:
        raise ValueError(f"Stance must be one of {', '.join(STANCES)}")
    if len(set(link_ids)) != len(link_ids):
        raise ValueError("link_ids must be unique")
    if not link_ids and stance not in UNCITED_STANCES:
        raise ValueError("An assessment must cite at least one live link")
    live = set(live_link_ids)
    if any(link not in live for link in link_ids):
        raise ValueError("Every cited link must be a live link of this version")
    cited = set(link_ids)
    if any(link not in cited for link in observation_link_ids):
        raise ValueError("Every observation must belong to a cited link")


def link_source_changed(
    link_text_sha: str | None,
    current_text_sha: str | None,
    accepted_is_tip: bool,
    accepted_stale: bool,
) -> bool:
    """Derived on read, never stored (GOO-305's rule)."""
    moved = link_text_sha is not None and link_text_sha != current_text_sha
    return moved or not accepted_is_tip or accepted_stale
