"""Pure source-anchor rules for extraction observations (GOO-305): no database.

Offsets are Python ``str`` indices (code points), ``[start, end)``, into
``Document.content_text`` exactly as stored. Nothing here normalizes text:
``text[start:end] == quote`` must hold for every verified anchor.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from src.services.research import extraction_rules as rules
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


# ponytail: fixed 8x12k cap; make it a setting only if real matrices hit it.
CHUNK_CHARS, OVERLAP, MAX_CHUNKS = 12_000, 500, 8
Range = tuple[int, int]


def plan_windows(n: int) -> list[Range]:
    """Windows reaching the end of any text; evenly spread when over the cap."""
    if n <= 0:
        return []
    if n <= CHUNK_CHARS:
        return [(0, n)]
    step = CHUNK_CHARS - OVERLAP
    k = math.ceil((n - CHUNK_CHARS) / step) + 1
    if k <= MAX_CHUNKS:
        return [(i * step, min(i * step + CHUNK_CHARS, n)) for i in range(k)]
    span = n - CHUNK_CHARS
    starts = [round(i * span / (MAX_CHUNKS - 1)) for i in range(MAX_CHUNKS)]
    return [(s, s + CHUNK_CHARS) for s in starts]


def merge_ranges(ranges: Sequence[Range]) -> list[Range]:
    merged: list[Range] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


@dataclass(frozen=True)
class Candidate:
    field_id: str
    value: Any
    missingness: str | None
    validation_state: str
    citation: str | None
    anchor: Anchor | None


def anchor_candidates(
    fields: Sequence[Mapping[str, Any]],
    parsed_chunk: Mapping[str, Mapping[str, Any]],
    window: Range,
    text: str,
) -> list[Candidate]:
    """One candidate per field from one window's parsed output; a value's
    citation is verified inside that window, with global offsets."""
    candidates = []
    for field in fields:
        entry = parsed_chunk.get(field["name"]) or {"missing": "extraction_error"}
        value, missingness, state = rules.normalize(
            "machine", field, entry.get("value"), entry.get("missing")
        )
        citation = entry.get("citation")
        citation = citation if isinstance(citation, str) else None
        anchor = None if missingness else verify_anchor(text, citation, window=window)
        candidates.append(
            Candidate(field["field_id"], value, missingness, state, citation, anchor)
        )
    return candidates


def aggregate(
    field_id: str, candidates: Sequence[Candidate], coverage_complete: bool
) -> list[Candidate]:
    """One observation per distinct value; automation never picks a winner.

    Without any value: extraction_error > not_applicable > not_reported (only
    after reading the whole text) > unavailable_text.
    """
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.missingness is None:
            key = str(candidate.value).strip().casefold()
            groups.setdefault(key, []).append(candidate)
    if groups:
        return [
            next(
                (c for c in group if c.anchor and c.anchor.status == "verified"),
                group[0],
            )
            for group in groups.values()
        ]
    said = {c.missingness for c in candidates}
    if "extraction_error" in said:
        missingness = "extraction_error"
    elif "not_applicable" in said:
        missingness = "not_applicable"
    elif coverage_complete:
        missingness = "not_reported"
    else:
        missingness = "unavailable_text"
    citation = next(
        (c.citation for c in candidates if c.missingness == missingness), None
    )
    return [Candidate(field_id, None, missingness, "valid", citation, None)]


async def read_whole_text(
    text: str,
    fields: Sequence[Mapping[str, Any]],
    read: Callable[[str], Awaitable[Mapping[str, Mapping[str, Any]]]],
) -> tuple[dict[str, list[Candidate]], list[Range], bool]:
    """Read every window of ``text`` (one ``read`` call each) and aggregate.

    -> (field_id -> observations to write, inspected coverage, complete).
    """
    windows = plan_windows(len(text))
    found: list[Candidate] = []
    for lo, hi in windows:
        found += anchor_candidates(fields, await read(text[lo:hi]), (lo, hi), text)
    coverage = merge_ranges(windows)
    complete = coverage == [(0, len(text))]
    return (
        {
            f["field_id"]: aggregate(
                f["field_id"],
                [c for c in found if c.field_id == f["field_id"]],
                complete,
            )
            for f in fields
        },
        coverage,
        complete,
    )
