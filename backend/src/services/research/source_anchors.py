"""Pure source-anchor rules for extraction observations (GOO-305): no database.

Offsets are Python ``str`` indices (code points), ``[start, end)``, into
``Document.content_text`` exactly as stored. Nothing here normalizes text:
``text[start:end] == quote`` must hold for every verified anchor.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from src.services.research.evidence_selection import _PAGE_MARKER_RE

AnchorStatus = Literal["verified", "ambiguous", "unverified", "location_unavailable"]
MAX_OCCURRENCES = 20


@dataclass(frozen=True)
class Anchor:
    status: AnchorStatus
    start_char: int | None = None
    end_char: int | None = None
    page: int | None = None
    occurrences: tuple[int, ...] = ()
    occurrences_in_text: int = 0


def text_sha256(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_all(text: str, quote: str, lo: int, hi: int, cap: int | None) -> list[int]:
    """Overlapping starts of ``quote`` lying wholly inside ``text[lo:hi]``."""
    found: list[int] = []
    at = text.find(quote, lo, hi)
    while at != -1 and (cap is None or len(found) < cap):
        found.append(at)
        at = text.find(quote, at + 1, hi)
    return found


def page_at(text: str, offset: int) -> int | None:
    """The last ``[Page N]`` marker at or before ``offset``; None without one."""
    page = None
    for match in _PAGE_MARKER_RE.finditer(text):
        if match.start() > offset:
            break
        page = int(match.group(1))
    return page


def context(text: str, start: int, end: int, radius: int = 300) -> tuple[str, str]:
    return text[max(0, start - radius) : start], text[end : end + radius]


def verify_anchor(
    text: str | None,
    quote: str | None,
    *,
    window: tuple[int, int] | None = None,
    start_hint: int | None = None,
) -> Anchor:
    """Locate ``quote`` in ``text``; ambiguity is counted inside ``window``.

    A ``start_hint`` wins only when the text at that offset is the quote; a
    mismatching hint is ``unverified`` and is never silently re-searched.
    """
    if not text:
        return Anchor("location_unavailable")
    if not quote or not quote.strip():
        return Anchor("unverified")
    in_text = len(_find_all(text, quote, 0, len(text), None))

    def located(start: int) -> Anchor:
        return Anchor(
            "verified",
            start,
            start + len(quote),
            page_at(text, start),
            (start,),
            in_text,
        )

    if start_hint is not None:
        if start_hint >= 0 and text[start_hint : start_hint + len(quote)] == quote:
            return located(start_hint)
        return Anchor("unverified", occurrences_in_text=in_text)
    lo, hi = window or (0, len(text))
    starts = _find_all(text, quote, lo, hi, MAX_OCCURRENCES)
    if not starts:
        return Anchor("unverified", occurrences_in_text=in_text)
    if len(starts) == 1:
        return located(starts[0])
    return Anchor("ambiguous", occurrences=tuple(starts), occurrences_in_text=in_text)
