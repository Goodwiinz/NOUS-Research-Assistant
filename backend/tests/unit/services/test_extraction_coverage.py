"""GOO-305 chunked whole-text extraction coverage (pure)."""

from typing import Any, Callable, Mapping

import pytest

from src.services.research.source_anchors import (
    CHUNK_CHARS,
    MAX_CHUNKS,
    OVERLAP,
    Anchor,
    Candidate,
    aggregate,
    merge_ranges,
    plan_windows,
    read_whole_text,
)

pytestmark = pytest.mark.unit

FIELD = {"field_id": "f-1", "name": "Sample size", "type": "text"}
SENTENCE = "We enrolled 412 participants."


def _filler(n: int) -> str:
    return ("lorem ipsum " * (n // 12 + 1))[:n]


def _candidate(value: Any = None, missing: str | None = None, **kw: Any) -> Candidate:
    return Candidate(
        "f-1", value, missing, "valid", kw.get("citation"), kw.get("anchor")
    )


async def _run(
    text: str, reply: Callable[[str], Mapping[str, Mapping[str, Any]]]
) -> tuple[dict[str, list[Candidate]], Any, bool]:
    async def read(chunk: str) -> Mapping[str, Mapping[str, Any]]:
        return reply(chunk)

    return await read_whole_text(text, [FIELD], read)


def test_windows_cover_whole_text_under_cap() -> None:
    for n in (1, CHUNK_CHARS, CHUNK_CHARS + 1, 60_000, 90_000):
        windows = plan_windows(n)
        assert merge_ranges(windows) == [(0, n)]
        assert len(windows) <= MAX_CHUNKS
        for (_, end), (start, _) in zip(windows, windows[1:]):
            assert 0 < end - start <= OVERLAP
        assert all(hi - lo <= CHUNK_CHARS for lo, hi in windows)
    assert plan_windows(0) == []


async def test_late_document_evidence_reachable() -> None:
    text = _filler(55_000) + SENTENCE + _filler(60_000 - 55_000 - len(SENTENCE))
    assert len(text) == 60_000 and text.index(SENTENCE) == 55_000

    def reply(chunk: str) -> dict[str, Any]:
        if SENTENCE in chunk:
            return {"Sample size": {"value": "412", "citation": SENTENCE}}
        return {"Sample size": {"missing": "not_reported"}}

    observations, coverage, complete = await _run(text, reply)
    (only,) = observations["f-1"]
    assert only.value == "412" and only.anchor is not None
    assert (only.anchor.status, only.anchor.start_char) == ("verified", 55_000)
    assert (coverage, complete) == ([(0, 60_000)], True)


async def test_over_cap_spreads_windows_and_reports_partial_coverage() -> None:
    n = 200_000
    windows = plan_windows(n)
    assert len(windows) == MAX_CHUNKS
    assert windows[0][0] == 0 and windows[-1][1] == n
    coverage = merge_ranges(windows)
    assert len(coverage) == MAX_CHUNKS  # gaps between every window
    _, _, complete = await _run(
        _filler(n), lambda _: {"Sample size": {"missing": "not_reported"}}
    )
    assert complete is False


async def test_partial_coverage_without_value_is_unavailable_text_not_not_reported() -> (
    None
):
    observations, _, complete = await _run(
        _filler(200_000), lambda _: {"Sample size": {"missing": "not_reported"}}
    )
    assert complete is False
    assert [o.missingness for o in observations["f-1"]] == ["unavailable_text"]


async def test_chunk_parse_error_without_value_elsewhere_is_extraction_error() -> None:
    calls: list[int] = []

    def reply(_: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 2:
            return {}  # this window's output lacked the field
        return {"Sample size": {"missing": "not_reported"}}

    observations, _, complete = await _run(_filler(30_000), reply)
    assert complete is True
    assert [o.missingness for o in observations["f-1"]] == ["extraction_error"]


def test_explicit_not_reported_only_when_coverage_complete() -> None:
    said = [_candidate(missing="not_reported")]
    assert aggregate("f-1", said, True)[0].missingness == "not_reported"
    assert aggregate("f-1", said, False)[0].missingness == "unavailable_text"
    applicable = said + [_candidate(missing="not_applicable")]
    assert aggregate("f-1", applicable, False)[0].missingness == "not_applicable"


def test_distinct_values_across_chunks_become_separate_observations() -> None:
    out = aggregate(
        "f-1",
        [_candidate("412"), _candidate(missing="not_reported"), _candidate("400")],
        True,
    )
    assert [o.value for o in out] == ["412", "400"]


def test_same_value_in_overlap_dedups_to_one() -> None:
    verified = Anchor("verified", 10, 13)
    out = aggregate(
        "f-1",
        [
            _candidate("412 ", anchor=Anchor("unverified")),
            _candidate("412", anchor=verified),
        ],
        True,
    )
    assert len(out) == 1 and out[0].anchor == verified
