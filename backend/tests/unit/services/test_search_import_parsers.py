"""Bounded, deterministic parsers for externally exported search results (GOO-300)."""

from pathlib import Path

import pytest

from src.services.research_engine import search_import
from src.services.research_engine.search_import import ImportFormatError, parse

FIXTURES = Path(__file__).parents[2] / "fixtures" / "search_import"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_ris_partial_file_accounting_is_deterministic() -> None:
    first = parse("ris", _fixture("partial.ris"))

    assert [r.rejection_reason for r in first] == [
        None,
        None,
        None,
        "missing_title",
        "unterminated_record",
    ]
    assert [r.index for r in first] == [0, 1, 2, 3, 4]
    assert [r.parsed.get("title") for r in first[:3]] == [
        "Record one",
        "Record two",
        "Record three",
    ]
    assert first[0].parsed["identifiers"] == {"doi": "10.1000/one"}
    assert first[0].parsed["year"] == "2018"
    assert parse("ris", _fixture("partial.ris")) == first


def test_rejected_record_keeps_original_raw() -> None:
    records = parse("ris", _fixture("partial.ris"))

    assert records[3].raw == ("TY  - JOUR\nAU  - No Title, X\nPY  - 2017\nER  - \n")
    assert records[4].raw == ("TY  - JOUR\nTI  - Record five never ends\nPY  - 2016\n")


def test_ris_maps_fields_and_notes_dropped_identifiers() -> None:
    first, second = parse("ris", _fixture("valid.ris"))

    assert first.rejection_reason is None
    assert first.parsed["authors"] == ["Smith, J", "Doe, A"]
    assert first.parsed["year"] == "2019"
    assert first.parsed["venue"] == "Lancet"
    assert first.parsed["identifiers"] == {"doi": "10.1000/abc123"}
    # AN is database-specific: kept verbatim, never mapped to pmid.
    assert first.parsed["fields"]["AN"] == ["EMB-0001"]
    assert "pmid" not in first.parsed["identifiers"]
    assert second.rejection_reason is None
    assert second.parsed["identifiers"] == {}
    assert second.parsed["dropped_identifiers"] == [
        {"kind": "doi", "value": "not-a-doi"}
    ]


def test_unknown_format_and_non_utf8_raise_explicit_codes() -> None:
    with pytest.raises(ImportFormatError) as unknown:
        parse("bibtex", b"@article{x}")
    assert (unknown.value.code, unknown.value.status) == ("unsupported_format", 422)

    with pytest.raises(ImportFormatError) as encoding:
        parse("ris", "TY  - JOUR\nTI  - caf\xe9\nER  - \n".encode("latin-1"))
    assert (encoding.value.code, encoding.value.status) == (
        "unsupported_encoding",
        422,
    )


def test_bom_is_stripped() -> None:
    (record,) = parse("ris", b"\xef\xbb\xbfTY  - JOUR\nTI  - Bom\nER  - \n")

    assert record.parsed["title"] == "Bom"


def test_too_many_records_and_file_too_large_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = "title\n" + "".join(f"Paper {i}\n" for i in range(5_001))
    with pytest.raises(ImportFormatError) as many:
        parse("csv", rows.encode())
    assert (many.value.code, many.value.status) == ("too_many_records", 413)

    monkeypatch.setattr(search_import, "MAX_BYTES", 10)
    with pytest.raises(ImportFormatError) as large:
        parse("csv", b"title\nlong enough\n")
    assert (large.value.code, large.value.status) == ("file_too_large", 413)


def test_empty_file_has_no_records() -> None:
    with pytest.raises(ImportFormatError) as empty:
        parse("ris", b"\n\n")
    assert empty.value.code == "no_records"


def test_oversized_record_is_rejected_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_import, "MAX_RECORD_BYTES", 30)
    records = parse("ris", b"TY  - JOUR\nTI  - " + b"x" * 40 + b"\nER  - \n")

    assert [r.rejection_reason for r in records] == ["record_too_large"]


def test_ris_line_without_tag_is_malformed() -> None:
    (record,) = parse("ris", b"TY  - JOUR\nTI  - Fine\nstray text\nER  - \n")

    assert record.rejection_reason == "malformed_line"
    assert "stray text" in record.raw


def test_nbib_maps_pmid_pmcid_doi() -> None:
    first, second = parse("nbib", _fixture("valid.nbib"))

    assert first.rejection_reason is None
    assert first.parsed["title"] == (
        "A MEDLINE record title that wraps onto a second line."
    )
    assert first.parsed["identifiers"] == {
        "doi": "10.1000/nbib1",
        "pmid": "31000001",
        "pmcid": "PMC6000001",
    }
    assert first.parsed["year"] == "2019"
    assert first.parsed["authors"] == ["Smith, John"]
    assert second.parsed["identifiers"] == {"pmid": "31000002"}
    assert second.raw.startswith("PMID- 31000002")


def test_csv_maps_known_columns_and_keeps_raw_line() -> None:
    first, second, third = parse("csv", _fixture("valid.csv"))

    assert first.parsed["identifiers"] == {"doi": "10.1000/csv1", "pmid": "12345678"}
    assert first.parsed["authors"] == ["Smith, J", "Doe, A"]
    assert "internal-note" in first.raw
    assert "internal-note" not in str(first.parsed)
    assert second.parsed["title"] == "Second, with comma"
    assert third.rejection_reason == "missing_title"
    assert third.raw == ",Nobody,2023,,,\n"


def test_csv_without_title_column_raises() -> None:
    with pytest.raises(ImportFormatError) as missing:
        parse("csv", b"doi,year\n10.1000/x,2020\n")
    assert missing.value.code == "csv_missing_title_column"


def test_no_query_or_date_inferred_from_file() -> None:
    for fmt, name in (
        ("ris", "valid.ris"),
        ("nbib", "valid.nbib"),
        ("csv", "valid.csv"),
    ):
        for record in parse(fmt, _fixture(name)):
            assert not {"query", "query_text", "search_date"} & set(record.parsed)
