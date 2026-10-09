"""GOO-299: report identity is decided by identifiers only, and explains itself."""

import json
from itertools import combinations
from pathlib import Path
from typing import Any

from src.services.research_engine.report_identity import (
    IDENTITY_KINDS,
    MAX_IDENTIFIER_LENGTH,
    assign_report,
    report_identifiers,
)

GOLD_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "report_identity_gold.json"
)


def cluster_records(records: list[dict[str, Any]]) -> dict[str, str]:
    """Fold ``assign_report`` over records exactly like the service does."""
    index: dict[tuple[str, str], str] = {}
    clusters: dict[str, str] = {}
    for record in records:
        if record["connector_type"] == "rag_store":
            continue
        hit = assign_report(index, record["identifiers"])
        key = hit.report_key or record["id"]
        clusters[record["id"]] = key
        for kind, value in report_identifiers(record["identifiers"]).items():
            if kind in IDENTITY_KINDS:
                index.setdefault((kind, value), key)
    return clusters


def predicted_pairs(clusters: dict[str, str]) -> set[frozenset[str]]:
    return {
        frozenset((a, b))
        for a, b in combinations(sorted(clusters), 2)
        if clusters[a] == clusters[b]
    }


def gold_pairs(gold: dict[str, Any]) -> set[frozenset[str]]:
    records = [r for r in gold["records"] if r["connector_type"] != "rag_store"]
    return {
        frozenset((a["id"], b["id"]))
        for a, b in combinations(records, 2)
        if a["gold"] == b["gold"]
    }


def test_arxiv_versions_share_base_but_keep_version_in_evidence() -> None:
    index = {("arxiv_base", "2301.00001"): "r1"}
    hit = assign_report(index, {"arxiv": "2301.00001v2"})
    assert hit.report_key == "r1" and hit.match_method == "arxiv_base"
    assert hit.evidence["matched"] == {"kind": "arxiv_base", "value": "2301.00001"}
    assert hit.evidence["observed"]["arxiv"] == "2301.00001v2"


def test_conflicting_identifiers_attach_by_priority_and_record_conflict() -> None:
    index = {("doi", "10.1000/a"): "r1", ("pmid", "99"): "r2"}
    hit = assign_report(index, {"doi": "10.1000/a", "pmid": "99"})
    assert hit.report_key == "r1"
    assert hit.evidence["conflicts"] == [
        {"kind": "pmid", "value": "99", "report_key": "r2"}
    ]


def test_title_alone_never_matches() -> None:
    assert assign_report({}, {"title": "Attention Is All You Need"}).report_key is None


def test_identifiers_are_normalized_before_matching() -> None:
    ids = report_identifiers({"doi": "https://doi.org/10.1109/CVPR.2016.90"})
    assert ids == {"doi": "10.1109/cvpr.2016.90"}


def test_gold_fixture_precision_and_recall() -> None:
    gold = json.loads(GOLD_PATH.read_text())
    clusters = cluster_records(gold["records"])  # pure, in-memory
    pairs = predicted_pairs(clusters)
    truth = gold_pairs(gold)
    precision = len(pairs & truth) / len(pairs)
    recall = len(pairs & truth) / len(truth)
    print(f"precision={precision:.3f} recall={recall:.3f}")
    assert precision == 1.0  # zero false merges
    assert recall >= gold["min_recall"]  # identifier-only ceiling
    for a, b in gold["known_false_merges"]:
        assert clusters[a] != clusters[b]


def test_overlong_identifiers_are_dropped_not_truncated() -> None:
    """The index key and the persisted ``String(512)`` value must be the same
    string, so an identifier that cannot be stored whole is not an identity key."""
    prefix = "10.1000/" + "a" * 600
    first = {"doi": prefix + "x", "pmid": "1"}
    second = {"doi": prefix + "y", "pmid": "2"}
    assert report_identifiers(first) == {"pmid": "1"}
    exact = {"doi": "10.1000/" + "a" * (MAX_IDENTIFIER_LENGTH - 8)}
    assert report_identifiers(exact) == exact
    clusters = cluster_records(
        [
            {"id": "s1", "connector_type": "crossref", "identifiers": first},
            {"id": "s2", "connector_type": "crossref", "identifiers": second},
        ]
    )
    assert clusters["s1"] != clusters["s2"]
