"""Deterministic, bounded evidence selection for draft and citation prompts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

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


# Verifier models wrap quotes in narrative ("The excerpt states: “…”"). A
# candidate span must have this many words to ground a verdict: shorter text
# ("Transformer", "e") is too generic to prove it came from the source.
_MIN_QUOTED_SPAN_WORDS = 4
_QUOTED_SPAN_RE = re.compile(r"[\"“«]([^\"“”«»]+)[\"”»]|‘([^‘’]+)’")
_QUOTE_CHARS = "\"'“”‘’«»`"
_QUOTE_FOLD = str.maketrans(
    {"“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'"}
)
# ponytail: keyword heuristic, fail-closed. Narrative that negates or disputes
# its own quote ("the excerpt does not say “…”") never grounds a verdict; the
# verbatim ``quote`` field is the primary path, this is only the fallback.
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|none|nor|neither|cannot|without|fails?|lacks?|absent|"
    r"contradict\w*|contrary|unsupported|refut\w*|dispute\w*)\b|n't\b",
    re.I,
)


def _normalize_for_match(text: str) -> str:
    return " ".join(text.translate(_QUOTE_FOLD).lower().split())


def _long_enough(text: str) -> bool:
    return len(text.split()) >= _MIN_QUOTED_SPAN_WORDS


def _grounded_span(evidence: str, occurs: Callable[[str], bool]) -> str | None:
    """Return the verbatim span of ``evidence`` that grounds it, else ``None``.

    1. The whole evidence (minus surrounding quote marks) when it is itself a
       verbatim quote of at least ``_MIN_QUOTED_SPAN_WORDS`` words.
    2. Otherwise narrative with quoted spans: EVERY quoted span of at least
       ``_MIN_QUOTED_SPAN_WORDS`` words must occur verbatim, and the narrative
       around them must not negate them. One real span can therefore never
       carry a fabricated one, and the returned span, never the narrative, is
       what callers store.
    Narrative without quote marks never grounds.
    """
    bare = evidence.strip().strip(_QUOTE_CHARS).strip()
    if _long_enough(bare) and occurs(bare):
        return bare
    spans = [
        span
        for match in _QUOTED_SPAN_RE.finditer(evidence)
        if _long_enough(span := (match.group(1) or match.group(2) or "").strip())
    ]
    if not spans:
        return None
    narrative = _QUOTED_SPAN_RE.sub(" ", evidence).translate(_QUOTE_FOLD)
    if _NEGATION_RE.search(narrative):
        return None
    if all(occurs(span) for span in spans):
        return max(spans, key=len)
    return None


def verbatim_evidence(source: str | None, evidence: str | None) -> str | None:
    """The span of ``evidence`` found verbatim in ``source`` (see ``_grounded_span``)."""
    if not source or not evidence:
        return None
    haystack = _normalize_for_match(str(source))
    return _grounded_span(
        str(evidence), lambda span: _normalize_for_match(span) in haystack
    )


def evidence_location(
    source: str | None, evidence: str | None
) -> tuple[int | None, str, str | None]:
    """Locate decisive evidence: ``(page, location, verbatim span)``.

    ``"source excerpt"`` with a ``None`` span means nothing verbatim was found
    in ``source``; the draft persistence gate treats that as ungrounded.
    """
    if not source or not evidence:
        return None, "source excerpt", None
    passages = [
        (passage, _normalize_for_match(passage.text))
        for passage in _passages(str(source))
    ]

    def find(span: str) -> _Passage | None:
        needle = _normalize_for_match(span)
        return next((p for p, text in passages if needle in text), None)

    span = _grounded_span(str(evidence), lambda value: find(value) is not None)
    passage = find(span) if span is not None else None
    if span is None or passage is None:
        return None, "source excerpt", None
    if passage.page is not None:
        return passage.page, f"Page {passage.page}", span
    return None, "legacy unanchored text", span
