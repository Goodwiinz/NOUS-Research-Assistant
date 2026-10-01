"""Pure peer-review rules (GOO-314): no database, no FastAPI.

Anchors use GOO-306's convention: offsets are Python ``str`` indices (code
points) into one saved draft version, and ``quote == content[start:end]``.
A later revision re-anchors a comment only when its quote occurs exactly
once; otherwise the comment is ``unresolved_anchor`` and keeps its original
quote and version. The anchored diff runs over GOO-307's sentence spans so it
lines up with the release gate. Callers turn ``ValueError`` into a 422.
"""

from __future__ import annotations

import difflib
import hashlib
from typing import Any, Literal, Sequence

from src.services.research.release_rules import assertion_spans
from src.services.research_engine.contracts import canonical_json_sha256

AnchorState = Literal["exact", "carried", "unresolved_anchor", "general"]
Status = Literal["open", "responded", "resolved"]
RESPONSE_KINDS = ("change", "no_change")
DECISION_KINDS = ("assigned", "resolved", "reopened")
# The resolution chain; ``assigned`` is its own chain.
RESOLUTION_KINDS = frozenset({"resolved", "reopened"})
QUOTE_MISMATCH = "Quote does not match the draft passage"
UNTOUCHED = "Revision does not touch the commented passage"
NO_DIFF = "Revision does not change the draft"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_anchor(content: str, start: int, end: int, quote: str) -> str:
    """Validate an anchor against the reviewed content; return ``quote_sha256``."""
    if not 0 <= start < end <= len(content):
        raise ValueError("Anchor offsets are out of range")
    if not quote.strip():
        raise ValueError("Anchor quote is blank")
    if content[start:end] != quote:
        raise ValueError(QUOTE_MISMATCH)
    return sha256(quote)


def anchor_state(
    quote: str | None,
    quote_sha256: str | None,
    origin_hash: str,
    start: int | None,
    end: int | None,
    target: str,
    target_hash: str,
) -> tuple[AnchorState, int | None, int | None]:
    """Where a comment's anchor sits in ``target`` (derived, never stored)."""
    if quote is None:
        return "general", None, None
    if quote_sha256 is not None and sha256(quote) != quote_sha256:
        raise ValueError("Anchor quote hash mismatch")
    if target_hash == origin_hash:
        return "exact", start, end
    first = target.find(quote)
    # ponytail: exact-once only, never fuzzy; a reviewed re-anchor is a new
    # comment version if pilots need one.
    if first == -1 or target.find(quote, first + 1) != -1:
        return "unresolved_anchor", None, None
    return "carried", first, first + len(quote)


def _range(spans: Sequence[tuple[int, int, str]], lo: int, hi: int) -> list[int]:
    if lo < hi:
        return [spans[lo][0], spans[hi - 1][1]]
    point = spans[lo - 1][1] if lo > 0 else 0  # insertion point
    return [point, point]


def anchored_diff(old: str, new: str) -> list[dict[str, Any]]:
    """Sentence-level hunks with character offsets on both sides.

    Only non-``equal`` hunks; ``old_text``/``new_text`` are the exact slices.
    """
    old_spans, new_spans = assertion_spans(old), assertion_spans(new)
    matcher = difflib.SequenceMatcher(
        None,
        [text for _, _, text in old_spans],
        [text for _, _, text in new_spans],
        autojunk=False,
    )
    hunks: list[dict[str, Any]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        old_range, new_range = _range(old_spans, i1, i2), _range(new_spans, j1, j2)
        hunks.append(
            {
                "op": op,
                "old": old_range,
                "new": new_range,
                "old_text": old[old_range[0] : old_range[1]],
                "new_text": new[new_range[0] : new_range[1]],
            }
        )
    return hunks


def diff_sha256(hunks: Sequence[dict[str, Any]]) -> str:
    return canonical_json_sha256(list(hunks))


def touches(hunks: Sequence[dict[str, Any]], start: int, end: int) -> bool:
    """Does any hunk overlap the base-side span ``[start, end)``?

    A pure insertion touches when it lands inside or at an edge of the span.
    """
    for hunk in hunks:
        lo, hi = hunk["old"]
        if lo == hi:
            if start <= lo <= end:
                return True
        elif lo < end and start < hi:
            return True
    return False


def check_response_shape(
    kind: str, revised_draft_id: Any, rationale: str | None
) -> None:
    """Mirror of the ``peer_review_responses`` CHECKs: a change links a
    revision and carries no rationale; a no-change needs a rationale."""
    if kind not in RESPONSE_KINDS:
        raise ValueError("Unknown response kind")
    if kind == "change":
        if revised_draft_id is None:
            raise ValueError("A change response requires revised_draft_id")
        if rationale is not None:
            raise ValueError("A change response must not carry a rationale")
    else:
        if revised_draft_id is not None:
            raise ValueError("A no-change response must not link a revision")
        if rationale is None or not rationale.strip():
            raise ValueError("A no-change response requires a rationale")


def comment_status(
    response_tip_id: Any, resolution_tip: tuple[str, Any] | None
) -> Status:
    """Derived status. ``resolution_tip`` is ``(kind, response_id)`` of the
    resolution chain's tip; only a ``resolved`` naming the current response
    tip resolves the comment."""
    if response_tip_id is None:
        return "open"
    if (
        resolution_tip is not None
        and resolution_tip[0] == "resolved"
        and str(resolution_tip[1]) == str(response_tip_id)
    ):
        return "resolved"
    return "responded"
