"""CSL JSON and RIS reference exports (GOO-317), checked by independent oracles.

Every generated file is parsed by something the serializer does not share:
CSL JSON by the vendored official CSL-data schema (``jsonschema``), RIS by
``tests/fixtures/references/ris_reader.py`` (no ``src`` import). Mutation
checks for the name, date, snippet and line-grammar guards are recorded in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-317).
"""

import hashlib
import importlib.util
import json
import unicodedata
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import Draft7Validator

from src.services.research.bibliography_service import BibliographyService as B

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/references"


def _records() -> list[dict[str, Any]]:
    raw = (FIXTURES / "records_v1.json").read_text(encoding="utf-8")
    return cast(list[dict[str, Any]], json.loads(raw)["records"])


def _keys(records: list[dict[str, Any]]) -> list[str]:
    return [str(r["key"]) for r in records]


def _case(name: str) -> dict[str, Any]:
    return next(r for r in _records() if r["case"] == name)


def _reader() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ris_reader", FIXTURES / "ris_reader.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator() -> Draft7Validator:
    schema = json.loads((FIXTURES / "csl-data.schema.json").read_text("utf-8"))
    return Draft7Validator(schema)


def _csl(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = json.loads(B.format_csl_json(records, _keys(records)))
    errors = [e.message for e in _validator().iter_errors(items)]
    assert not errors, errors
    return cast(list[dict[str, Any]], items)


def _ris(records: list[dict[str, Any]]) -> list[dict[str, list[str]]]:
    return cast(
        list[dict[str, list[str]]],
        _reader().parse(B.format_ris(records, _keys(records))),
    )


def test_csl_validates_against_official_schema_for_every_fixture() -> None:
    for record in _records():
        assert len(_csl([record])) == 1, record["case"]
    assert len(_csl(_records())) == len(_records())
    assert B.format_csl_json([], []) == "[]\n"


def test_ris_parses_with_independent_reader_for_every_fixture() -> None:
    for record in _records():
        assert len(_ris([record])) == 1, record["case"]
    assert len(_ris(_records())) == len(_records())
    assert B.format_ris([], []) == ""


def _nfc(value: Any) -> Any:
    return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


def test_round_trip_identifiers_keys_author_order_and_unicode() -> None:
    records = _records()
    csl, ris = _csl(records), _ris(records)
    assert [i["id"] for i in csl] == _keys(records)
    assert [r["ID"] for r in ris] == [[k] for k in _keys(records)]
    for record, item, entry in zip(records, csl, ris):
        authors = [_nfc(a) for a in record["authors"]]
        assert [a["literal"] for a in item.get("author", [])] == authors
        assert entry.get("AU", []) == authors
        assert item.get("title") == _nfc(record["title"])
        assert entry.get("TI", [None])[0] == _nfc(record["title"])
        assert item.get("DOI") == record["doi"]
        assert entry.get("DO", [None])[0] == record["doi"]
        assert item.get("container-title") == _nfc(record["venue"])
        assert entry.get("T2", [None])[0] == _nfc(record["venue"])
        arxiv = record["arxiv_id"]
        assert item.get("archive_location") == arxiv
        assert entry.get("AN", [None])[0] == (f"arXiv:{arxiv}" if arxiv else None)
        year = record["year"]
        assert item.get("issued") == ({"date-parts": [[year]]} if year else None)
        assert entry.get("PY", [None])[0] == (str(year) if year else None)
    assert csl[0]["author"][0]["literal"] == "Zoë Ångström"
    assert ris[5]["TI"] == ["日本語の文献管理入門"]


def test_ad_hoc_namespace_records_match_snapshot_mappings() -> None:
    """Draft exports pass ``SimpleNamespace(document_title=...)``; the same
    record as a snapshot mapping serializes to the same bytes."""
    records = _records()
    spaces = [
        SimpleNamespace(
            document_title=r["title"],
            authors=r["authors"],
            year=r["year"],
            venue=r["venue"],
            doi=r["doi"],
            arxiv_id=r["arxiv_id"],
            type=r["type"],
            abstract="never exported",
        )
        for r in records
    ]
    keys = _keys(records)
    assert B.format_csl_json(spaces, keys) == B.format_csl_json(records, keys)
    assert B.format_ris(spaces, keys) == B.format_ris(records, keys)
    assert "never exported" not in B.format_csl_json(spaces, keys)


def test_corporate_author_literal_not_split() -> None:
    record = _case("corporate")
    assert _csl([record])[0]["author"] == [{"literal": "World Health Organization"}]
    assert _ris([record])[0]["AU"] == ["World Health Organization"]


def test_structured_names_stay_structured() -> None:
    record = {**_case("journal"), "authors": [{"family": "Núñez", "given": "José"}]}
    assert _csl([record])[0]["author"] == [{"family": "Núñez", "given": "José"}]
    assert _ris([record])[0]["AU"] == ["Núñez, José"]


def test_absent_year_omits_issued_and_py_and_reports() -> None:
    record = _case("corporate")
    assert "issued" not in _csl([record])[0]
    assert "PY" not in _ris([record])[0]
    text = B.format_ris([record], ["doc2"]) + B.format_csl_json([record], ["doc2"])
    assert "n.d." not in text
    assert {
        "key": "doc2",
        "field": "year",
        "reason": "absent",
        "value": None,
    } in B.omissions([record], ["doc2"])


def test_snippet_never_becomes_title() -> None:
    record = _case("untitled_with_snippet")
    assert record["snippet"]
    csl, ris = _csl([record])[0], _ris([record])[0]
    assert "title" not in csl
    assert "TI" not in ris
    text = B.format_csl_json([record], ["doc4"]) + B.format_ris([record], ["doc4"])
    assert record["snippet"] not in text
    assert {
        "key": "doc4",
        "field": "title",
        "reason": "absent",
        "value": None,
    } in B.omissions([record], ["doc4"])


def test_unknown_type_is_document_gen_and_reported() -> None:
    record = _case("unknown_type")
    assert _csl([record])[0]["type"] == "document"
    assert _ris([record])[0]["TY"] == ["GEN"]
    assert {
        "key": "doc5",
        "field": "type",
        "reason": "type_unmapped",
        "value": "hologram",
    } in B.omissions([record], ["doc5"])
    mapped = [
        o for o in B.omissions(_records(), _keys(_records())) if o["field"] == "type"
    ]
    assert [o["key"] for o in mapped] == ["doc5"]


def test_type_mapping_table() -> None:
    expected = {
        "journal": ("article-journal", "JOUR"),
        "corporate": ("report", "RPRT"),
        "preprint": ("article", "UNPB"),
        "cjk": ("book", "BOOK"),
    }
    for case, (csl_type, ris_type) in expected.items():
        assert _csl([_case(case)])[0]["type"] == csl_type, case
        assert _ris([_case(case)])[0]["TY"] == [ris_type], case


def test_ris_crlf_utf8_no_bom() -> None:
    text = B.format_ris(_records(), _keys(_records()))
    data = text.encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert "\n" not in text.replace("\r\n", "")
    assert text.endswith("ER  - \r\n")
    multiline = {**_case("journal"), "title": "Line one\nline two"}
    assert _ris([multiline])[0]["TI"] == ["Line one line two"]


def test_keys_must_match_record_count() -> None:
    with pytest.raises(ValueError):
        B.format_ris(_records(), ["doc1"])
    with pytest.raises(ValueError):
        B.format_csl_json(_records(), [])


# Captured at 139646a2d (before the serializers existed) from the existing
# formatters over records_v1.json: the new formats must not move them.
GOLDEN = {
    "bibtex": "58217ffee4698cbf3ac3b948fb041eedd405a1f5822830762cff665335bb1673",
    "ieee": "ff65acb339c7c5e1454c2ebfbc2c8a114d8247723b95058e28499746dec7216a",
    "apa": "efdc3dd458520671336c4ec8ed7283e9cd5e73ef7eaeaf83938c99ebfc596840",
    "mla": "efa35eab926c05b0ae107c5647465472c211d9a698a41d9d105e93f9a1e57dc7",
}


def test_existing_bibtex_ieee_apa_mla_outputs_byte_identical() -> None:
    records = _records()
    citations: list[Any] = [
        SimpleNamespace(
            document_title=r["title"],
            authors=r["authors"] or None,
            year=r["year"],
            venue=r["venue"],
            doi=r["doi"],
            arxiv_id=r["arxiv_id"],
            abstract=None,
        )
        for r in records
    ]
    out = {
        "bibtex": B.format_bibtex(citations, _keys(records)),
        "ieee": B.format_ieee(citations),
        "apa": B.format_apa(citations),
        "mla": B.format_mla(citations),
    }
    for name, text in out.items():
        assert hashlib.sha256(text.encode()).hexdigest() == GOLDEN[name], name


def _snapshot() -> dict[str, Any]:
    keep = ("key", "type", "title", "authors", "year", "venue", "doi", "arxiv_id")
    return {"references": [{k: r[k] for k in keep} for r in _records()]}


def test_reference_mapping_reconciles_bib_csl_ris() -> None:
    from src.services.research import manuscript_release_service as svc
    from src.services.research import manuscript_rules as rules
    from src.services.research_engine import audit_bundle

    snapshot = _snapshot()
    bib = svc.references_bib(snapshot)
    csl = json.loads(svc.reference_file(snapshot, "csl-json")[0])
    ris = svc.reference_file(snapshot, "ris")[0]
    mapping = rules.reference_mapping(snapshot, bib, csl, ris)
    assert [k for k, _sha in mapping] == _keys(_records())
    with pytest.raises(ValueError, match="references.json"):
        rules.reference_mapping(snapshot, bib, list(reversed(csl)), ris)
    with pytest.raises(ValueError, match="references.ris"):
        rules.reference_mapping(
            snapshot, bib, csl, ris.replace("ID  - doc6", "ID  - x")
        )
    # The new members are valid bundle parts (every .json member sealed).
    bib_bytes = bib.encode()
    parts = [
        audit_bundle.Part("references.bib", None, bib_bytes, svc._sha(bib_bytes)),
        *svc._reference_parts(snapshot),
    ]
    data, _manifest = audit_bundle.write_zip(
        parts,
        project_id="p",
        generated_at="2026-10-01T00:00:00+00:00",
        deployment_sha=None,
        protocol_version_id=None,
        stream_heads={},
        schema=rules.PACKAGE_SCHEMA,
    )
    listed = audit_bundle.verify_bundle(data, schema=rules.PACKAGE_SCHEMA)["parts"]
    assert listed == [
        "references.bib",
        "references.json",
        "references.omissions.json",
        "references.ris",
    ]
    members = svc._members(data)
    assert json.loads(members["references.json"])["body"] == csl
    assert members["references.ris"].decode("utf-8") == ris
    omissions = json.loads(members["references.omissions.json"])["body"]
    assert omissions == svc.reference_omissions(snapshot)
