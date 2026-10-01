"""The pure GOO-306 claims export package (``claims_export``)."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from src.services.research import claim_rules
from src.services.research.claims_export import SCHEMA, build_body, package

pytestmark = pytest.mark.unit

CONTENT = "Intro. Trials reduced mortality by 12%. End."
PASSAGE = "Trials reduced mortality by 12%."
START = CONTENT.index(PASSAGE)
SOURCE = "The trial reported mortality fell by twelve percent overall."
QUOTE = "mortality fell by twelve percent"


def _rows() -> dict[str, Any]:
    t = "2026-09-30T10:00:0{}+00:00".format
    version = {
        "id": "v1",
        "claim_id": "c1",
        "draft_id": "d1",
        "draft_content_hash": claim_rules.content_hash(CONTENT),
        "start_char": START,
        "end_char": START + len(PASSAGE),
        "text": PASSAGE,
        "supersedes_claim_version_id": None,
        "created_at": t(1),
    }
    base_link = {
        "claim_version_id": "v1",
        "accepted_value_id": None,
        "draft_citation_id": None,
        "document_id": "doc1",
        "source_hash": "a" * 64,
        "text_sha256": "b" * 64,
        "start_char": None,
        "end_char": None,
        "quote": None,
        "status": "linked",
        "supersedes_link_id": None,
    }
    span_start = SOURCE.index(QUOTE)
    links = [
        base_link
        | {
            "id": "l1",
            "kind": "source_span",
            "start_char": span_start,
            "end_char": span_start + len(QUOTE),
            "quote": QUOTE,
            "created_at": t(2),
        },
        base_link
        | {
            "id": "l2",
            "kind": "extraction",
            "accepted_value_id": "av1",
            "created_at": t(3),
        },
        base_link
        | {
            "id": "l3",
            "kind": "legacy_unanchored",
            "draft_citation_id": "dc1",
            "source_hash": None,
            "text_sha256": None,
            "created_at": t(4),
        },
        base_link
        | {
            "id": "l4",
            "kind": "source_span",
            "status": "withdrawn",
            "supersedes_link_id": "l1",
            "start_char": span_start,
            "end_char": span_start + len(QUOTE),
            "quote": QUOTE,
            "created_at": t(5),
        },
    ]
    return {
        "collection_id": "p1",
        "draft_filter": None,
        "stream_head": 6,
        "drafts": [{"id": "d1", "version": 1, "content": CONTENT, "created_at": t(0)}],
        "documents": [
            {
                "id": "doc1",
                "title": "Trial",
                "source_hash": "a" * 64,
                "current_text_sha256": "b" * 64,
            }
        ],
        "accepted_values": [
            {
                "id": "av1",
                "observation_ids": ["o1", "o2"],
                "anchor_observation_id": "o2",
                "anchor_start_char": 4,
                "anchor_end_char": 9,
                "created_at": t(0),
            }
        ],
        "extraction_observations": [
            {"id": "o1", "citation": "x", "created_at": t(0)},
            {"id": "o2", "citation": "y", "created_at": t(0)},
        ],
        "claims": [{"id": "c1", "created_at": t(1)}],
        "claim_versions": [version],
        "links": links,
        "stance_observations": [
            {"id": "s1", "link_id": "l2", "stance": "supporting", "created_at": t(6)}
        ],
        "assessments": [],
    }


def test_body_hash_stable_under_row_order() -> None:
    rows = _rows()
    first = package(build_body(**rows), "2026-09-30T11:00:00+00:00")
    shuffled = {
        key: (
            random.Random(7).sample(value, len(value))
            if isinstance(value, list)
            else value
        )
        for key, value in rows.items()
    }
    second = package(build_body(**shuffled), "2026-10-01T00:00:00+00:00")
    assert first["schema"] == SCHEMA
    assert first["body_sha256"] == second["body_sha256"]
    assert first["body"] == second["body"]


def test_every_passage_resolves_by_slicing() -> None:
    body = build_body(**_rows())
    drafts = {d["id"]: d for d in body["drafts"]}
    versions = {v["id"]: v for v in body["claim_versions"]}
    assert body["resolution"]
    for row in body["resolution"]:
        draft_id, start, end = row["passage"]
        assert (
            drafts[draft_id]["content"][start:end]
            == versions[row["claim_version_id"]]["text"]
        )
    assert all(d["content_hash_matches"] for d in body["drafts"])
    by_link = {r["link_id"]: r for r in body["resolution"]}
    assert by_link["l2"]["observation_id"] == "o2"
    assert by_link["l2"]["span"] == [4, 9]


def test_counts_match_rows() -> None:
    body = build_body(**_rows())
    assert body["counts"] == {
        "claims": 1,
        "claim_versions": 1,
        "links_by_kind": {"extraction": 1, "source_span": 2, "legacy_unanchored": 1},
        "withdrawn_links": 1,
        "stance_observations": 1,
        "assessments_by_stance": {},
        "unassessed_tip_versions": 1,
    }
    # Withdrawn l4 and superseded l1 are not live: only l2 and l3 resolve.
    assert [r["link_id"] for r in body["resolution"]] == ["l2", "l3"]


def test_legacy_links_counted_and_unresolved() -> None:
    body = build_body(**_rows())
    (legacy,) = [r for r in body["resolution"] if r["kind"] == "legacy_unanchored"]
    assert body["counts"]["links_by_kind"]["legacy_unanchored"] == 1
    assert legacy["observation_id"] is None
    assert legacy["span"] is None
    assert legacy["source_hash"] is None and legacy["text_sha256"] is None


def test_no_document_text_in_package() -> None:
    rows = _rows()
    rows["documents"][0]["content_text"] = SOURCE  # a careless caller
    encoded = json.dumps(package(build_body(**rows), "t"))
    assert "twelve percent overall" not in encoded
    assert set(build_body(**_rows())["documents"][0]) == {
        "id",
        "title",
        "source_hash",
        "current_text_sha256",
    }
