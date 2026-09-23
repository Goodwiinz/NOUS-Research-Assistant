"""Deterministic, bounded evidence selection for draft and citation prompts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

_PAGE_MARKER_RE = re.compile(r"(?m)^\s*\[Page\s+(\d+)\]\s*$")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?", re.IGNORECASE)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


@dataclass(frozen=True)
class _Passage:
    text: str
    page: int | None
    order: int


def _tokens(values: Iterable[str]) -> set[str]:
    return {
        token.lower()
        for value in values
        for token in _TOKEN_RE.findall(value or "")
        if token.lower() not in _STOPWORDS
    }


def _chunk_text(text: str, *, page: int | None, start_order: int) -> list[_Passage]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    passages: list[_Passage] = []
    order = start_order
    for paragraph in paragraphs:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", paragraph)
            if sentence.strip()
        ]
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip()
            if current and len(candidate) > 900:
                passages.append(_Passage(current, page, order))
                order += 1
                current = sentence
            else:
                current = candidate
        if current:
            passages.append(_Passage(current, page, order))
            order += 1
    return passages


def _passages(source: str) -> list[_Passage]:
    matches = list(_PAGE_MARKER_RE.finditer(source))
    if not matches:
        return _chunk_text(source, page=None, start_order=0)

    passages: list[_Passage] = []
    order = 0
    legacy_prefix = source[: matches[0].start()].strip()
    if legacy_prefix:
        prefix = _chunk_text(legacy_prefix, page=None, start_order=order)
        passages.extend(prefix)
        order += len(prefix)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        page_passages = _chunk_text(
            source[match.end() : end],
            page=int(match.group(1)),
            start_order=order,
        )
        passages.extend(page_passages)
        order += len(page_passages)
    return passages


def select_relevant_passages(
    source: str | None,
    *,
    queries: Sequence[str],
    max_chars: int,
) -> str:
    """Select claim/theme-relevant passages without exceeding ``max_chars``.

    Existing page anchors follow their selected passages. Legacy unanchored text
    remains unanchored, so this function never manufactures a page number.
    """
    if not source or max_chars <= 0:
        return ""
    candidates = _passages(str(source))
    if not candidates:
        return ""
    query_tokens = _tokens(queries)

    def score(passage: _Passage) -> tuple[int, int, int]:
        overlap = query_tokens & _tokens([passage.text])
        numeric = sum(1 for token in overlap if any(char.isdigit() for char in token))
        return (numeric * 4 + len(overlap), len(overlap), -passage.order)

    scored = [(score(candidate), candidate) for candidate in candidates]
    has_relevant = bool(query_tokens) and any(item[0][0] > 0 for item in scored)
    ranked = [
        candidate
        for candidate_score, candidate in sorted(
            scored, key=lambda item: item[0], reverse=True
        )
        if not has_relevant or candidate_score[0] > 0
    ]
    chosen: list[_Passage] = []
    used = 0
    for passage in ranked:
        marker = f"[Page {passage.page}]\n" if passage.page is not None else ""
        separator = 2 if chosen else 0
        remaining = max_chars - used - separator
        if remaining <= len(marker):
            continue
        rendered_length = len(marker) + len(passage.text)
        if rendered_length <= remaining:
            chosen.append(passage)
            used += separator + rendered_length
        elif not chosen:
            clip_budget = remaining - len(marker)
            lowered = passage.text.lower()
            matches = [
                (lowered.find(token), len(token), token)
                for token in query_tokens
                if lowered.find(token) >= 0
            ]
            if matches:
                match_start, match_length, _ = min(
                    matches,
                    key=lambda match: (
                        -int(any(char.isdigit() for char in match[2])),
                        -match[1],
                        match[0],
                        match[2],
                    ),
                )
                start = max(0, match_start - max(0, (clip_budget - match_length) // 2))
                start = min(start, max(0, len(passage.text) - clip_budget))
                clipped = passage.text[start : start + clip_budget].strip()
            else:
                clipped = passage.text[:clip_budget].rstrip()
            if clipped:
                chosen.append(_Passage(clipped, passage.page, passage.order))
            break
    if not chosen:
        return ""
    chosen.sort(key=lambda passage: passage.order)
    return "\n\n".join(
        (f"[Page {passage.page}]\n" if passage.page is not None else "") + passage.text
        for passage in chosen
    )[:max_chars]


def evidence_location(
    source: str | None, evidence: str | None
) -> tuple[int | None, str]:
    """Return an inspectable page/location for a decisive evidence quote."""
    if not source or not evidence:
        return None, "source excerpt"
    needle = " ".join(str(evidence).lower().split())
    if not needle:
        return None, "source excerpt"
    for passage in _passages(str(source)):
        if needle in " ".join(passage.text.lower().split()):
            if passage.page is not None:
                return passage.page, f"Page {passage.page}"
            return None, "legacy unanchored text"
    return None, "source excerpt"
