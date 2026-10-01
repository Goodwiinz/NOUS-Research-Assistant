"""Pure GOO-319 rules: schedule fires, strategy hash and delta classes.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-319 section):
``due_fire`` returning the earliest missed fire with ``missed = 0`` fails
``-k coalesces``; ``_missing_reason`` answering ``corrected_retracted`` for a
work that was not returned fails ``-k disappeared``.
"""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from src.services.research_engine import search_update_rules as rules

LONDON = "Europe/London"


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _entry(doi: str | None = None, pmid: str | None = None, **fields: object) -> dict:
    identifiers = {k: v for k, v in (("doi", doi), ("pmid", pmid)) if v}
    record: dict[str, object] = {
        "title": "A study",
        "authors": ["Ada Lovelace"],
        "year": "2021",
    }
    record.update(fields)
    return rules.snapshot_entry(identifiers, record)


def _coverage(**providers: tuple[str, str]) -> dict:
    return {
        "providers": {
            name: {"status": status, "completion": completion}
            for name, (status, completion) in providers.items()
        }
    }


def test_due_fire_coalesces_missed_ticks_and_counts_them() -> None:
    cron, tz = rules.parse_schedule("0 6 * * 1", LONDON)
    created = datetime(2026, 1, 1, 9, 0)  # a Thursday
    # Four Mondays have passed (Jan 5, 12, 19, 26) by Jan 27.
    fire = rules.due_fire(cron, tz, None, created, _utc(2026, 1, 27, 12))
    assert fire is not None
    local, utc, missed = fire
    assert local == datetime(2026, 1, 26, 6, 0)
    assert utc == _utc(2026, 1, 26, 6)  # GMT in January
    assert missed == 3
    # Nothing is due again until the next Monday.
    assert rules.due_fire(cron, tz, local, created, _utc(2026, 2, 1, 12)) is None
    nxt = rules.due_fire(cron, tz, local, created, _utc(2026, 2, 2, 6, 1))
    assert nxt is not None and nxt[0] == datetime(2026, 2, 2, 6, 0) and nxt[2] == 0


def test_dst_fall_back_fires_once_spring_forward_fires_once() -> None:
    cron, tz = rules.parse_schedule("30 1 * * *", LONDON)
    # Fall back 2026-10-25: 01:30 happens twice (BST then GMT); one identity.
    before = datetime(2026, 10, 24, 1, 30)
    fall = rules.due_fire(cron, tz, before, before, _utc(2026, 10, 25, 3))
    assert fall is not None
    assert fall[0] == datetime(2026, 10, 25, 1, 30)
    assert fall[1] == _utc(2026, 10, 25, 0, 30)  # the first (BST) 01:30
    assert fall[2] == 0
    assert rules.due_fire(cron, tz, fall[0], before, _utc(2026, 10, 25, 23)) is None
    # Spring forward 2026-03-29: 01:30 does not exist; it fires once, forward.
    before = datetime(2026, 3, 28, 1, 30)
    spring = rules.due_fire(cron, tz, before, before, _utc(2026, 3, 29, 4))
    assert spring is not None and spring[2] == 0
    assert spring[0] == datetime(2026, 3, 29, 1, 30)
    assert spring[1] == _utc(2026, 3, 29, 1, 30)  # 02:30 BST
    assert rules.due_fire(cron, tz, spring[0], before, _utc(2026, 3, 29, 23)) is None


def test_sub_hourly_cron_rejected() -> None:
    with pytest.raises(ValueError, match="at most hourly"):
        rules.parse_schedule("*/15 * * * *", LONDON)
    with pytest.raises(ValueError):
        rules.parse_schedule("not a cron", LONDON)
    with pytest.raises(ValueError):
        rules.parse_schedule("0 6 * * 1", "Mars/Olympus")
    with pytest.raises(ValueError, match="never fires"):
        rules.parse_schedule("0 6 30 2 *", LONDON)


def test_strategy_hash_matches_step_executor_fixture() -> None:
    # The exact construction in step_executor._execute_search.
    strategy = {
        "schema_version": "nous.academic.search-strategy.v1",
        "project_id": "p",
        "protocol_version_id": "v",
        "effective_plan_hash": "h",
        "blueprint_id": "b",
        "blueprint_version": 1,
        "step_id": "search",
        "intended": {"selected_providers": ["crossref"], "parameters": {}},
        "route_limits": {"requested_results_per_provider": 20},
    }
    raw = json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()
    expected = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert rules.strategy_hash(strategy) == expected
    assert rules.strategy_hash({**strategy, "strategy_version": expected}) == expected


def test_disappeared_work_is_unknown_not_retracted() -> None:
    baseline = {"d": _entry(doi="10.1/d")}
    delta = rules.classify(
        baseline, {}, _coverage(crossref=("ok", "exhausted")), {"10.1/d": []}, set()
    )
    (item,) = delta["items"]
    assert item["class"] == "unknown" and item["reason"] == "not_returned"
    assert delta["counts"]["corrected_retracted"] == 0
    assert "deleted" not in json.dumps(delta)


def test_capped_provider_marks_unknown() -> None:
    baseline = {"c": _entry(pmid="111"), "d": _entry(doi="10.1/d")}
    coverage = _coverage(crossref=("ok", "exhausted"), pubmed=("ok", "cap_reached"))
    delta = rules.classify(baseline, {}, coverage, {"10.1/d": []}, set())
    reasons = {i["report_id"]: i["reason"] for i in delta["items"]}
    assert reasons == {"c": "provider_capped", "d": "not_returned"}
    failed = _coverage(crossref=("failed", "failed"))
    (item,) = rules.classify({"d": baseline["d"]}, {}, failed, {}, set())["items"]
    assert item["reason"] == "provider_failed"


def test_crossref_retraction_notice_classifies_corrected_retracted() -> None:
    entry = _entry(doi="10.1/a")
    notice = {"notice_doi": "10.1/a.retraction", "type": "retraction", "date": "2026"}
    delta = rules.classify(
        {"a": entry},
        {"a": entry},
        _coverage(crossref=("ok", "exhausted")),
        {"10.1/a": [notice]},
        set(),
    )
    (item,) = delta["items"]
    assert item["class"] == "corrected_retracted"
    assert item["evidence"]["notices"] == [notice]
    assert item["publication"]["source"] == "crossref"
    # A failed notice check never yields unchanged.
    outage = rules.classify(
        {"a": entry},
        {"a": entry},
        _coverage(crossref=("ok", "exhausted")),
        {},
        {"10.1/a"},
    )
    assert outage["items"][0]["class"] == "unknown"
    assert outage["items"][0]["reason"] == "provider_failed"


def test_same_doi_changed_key_is_changed() -> None:
    before = _entry(doi="10.1/b", provider_updated="2025-01-01T00:00:00Z")
    after = _entry(doi="10.1/b", provider_updated="2026-02-01T00:00:00Z")
    assert before["version_key"] != after["version_key"]
    delta = rules.classify(
        {"b": before},
        {"b": after},
        _coverage(crossref=("ok", "exhausted")),
        {"10.1/b": []},
        set(),
    )
    (item,) = delta["items"]
    assert item["class"] == "changed"
    assert list(item["evidence"]["fields"]) == ["provider_updated"]
    # A field only one side has is not evidence of change.
    redacted = rules.snapshot_entry({"doi": "10.1/b"}, {"title": "A study"})
    same = rules.classify(
        {"b": redacted},
        {"b": _entry(doi="10.1/b", title="A study")},
        _coverage(crossref=("ok", "exhausted")),
        {"10.1/b": []},
        set(),
    )
    assert same["items"][0]["class"] == "unchanged"


def test_merges_follow_survivor_and_unresolved_is_unknown() -> None:
    entry = _entry(doi="10.1/m")
    delta = rules.classify(
        {"old": entry, "split": _entry(doi="10.1/s")},
        {"new": entry},
        _coverage(crossref=("ok", "exhausted")),
        {"10.1/m": [], "10.1/s": []},
        set(),
        merges={"old": "new", "split": None},
    )
    by_id = {i["report_id"]: i for i in delta["items"]}
    assert by_id["new"]["class"] == "unchanged"
    assert by_id["new"]["evidence"]["merged_from"] == ["old"]
    assert by_id["split"]["reason"] == "merge_unresolved"
    assert delta["counts"] == {
        "new": 0,
        "changed": 0,
        "corrected_retracted": 0,
        "unchanged": 1,
        "unknown": 1,
    }
