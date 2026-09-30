"""Pure PRISMA 2020 flow derivation (GOO-303): counts come only from rows."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services.research_engine.prisma import (
    Attempt,
    Merge,
    Outcome,
    PrismaInconsistency,
    PrismaInputs,
    Record,
    Report,
    derive_prisma_flow,
    package,
    render_markdown,
)

VERSIONS = {
    "protocol_version_ids": ["v1"],
    "stream_heads": {"research_identity:c": 3, "research_acquisition:c": 5},
}


def _reports(*ids: UUID, **overrides: Report) -> tuple[Report, ...]:
    return tuple(overrides.get(str(i), Report(i, None, None, None)) for i in ids)


def _inputs(**fields: Any) -> PrismaInputs:
    defaults: dict[str, Any] = {
        "records": (),
        "rejected_imports": 0,
        "workspace_documents": 0,
        "reports": (),
        "outcomes": (),
        "attempts": (),
        "merges": (),
        "versions": VERSIONS,
    }
    return PrismaInputs(**{**defaults, **fields})


def _outcome(
    stage: str,
    report: UUID,
    decision: str | None,
    seq: int,
    *,
    reason: str | None = None,
    basis: str = "agreement",
    supersedes: bool = False,
) -> Outcome:
    return Outcome(stage, report, decision, reason, uuid4(), seq, supersedes, basis)


def _attempt(
    request: UUID,
    report: UUID,
    outcome: str | None,
    seq: int,
    previous: UUID | None = None,
    attempt_id: UUID | None = None,
) -> Attempt:
    if outcome is None:
        return Attempt(request, report, None, None, seq, None, None)
    return Attempt(
        request, report, attempt_id or uuid4(), outcome, seq, previous, uuid4()
    )


def test_in_run_merged_provenance_counts_each_provider_record() -> None:
    report = uuid4()
    body = derive_prisma_flow(
        _inputs(
            records=(
                Record("s1:0", "provider", "openalex", report),
                Record("s1:1", "provider", "pubmed", report),
                Record("i1", "import", "Embase", report),
            ),
            reports=_reports(report),
            workspace_documents=2,
            rejected_imports=1,
        )
    )
    counts = body["counts"]
    assert counts["records_by_source"] == {"openalex": 1, "pubmed": 1}
    assert counts["records_by_import"] == {"Embase": 1}
    assert counts["records_identified"] == 3
    assert counts["import_rejected"] == 1
    assert body["excluded_from_flow"] == {"workspace_documents": 2}
    assert counts["duplicates_removed"] == 2
    assert counts["unique_reports"] == 1


def test_duplicates_are_records_minus_unique_final_reports() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    body = derive_prisma_flow(
        _inputs(
            records=(
                Record("1", "provider", "openalex", a),
                Record("2", "provider", "openalex", b),  # b merged into a
                Record("3", "provider", "pubmed", c),
                Record("4", "provider", "pubmed", a),
            ),
            reports=(
                Report(a, None, None, None),
                Report(b, a, None, None),
                Report(c, None, None, None),
            ),
        )
    )
    assert body["counts"]["unique_reports"] == 2
    assert body["counts"]["duplicates_removed"] == 2
    assert body["counts"]["records_awaiting_screening"] == 2


def test_unavailable_is_not_retrieved_and_never_excluded() -> None:
    a, b = uuid4(), uuid4()
    request = uuid4()
    body = derive_prisma_flow(
        _inputs(
            records=(
                Record("1", "provider", "openalex", a),
                Record("2", "provider", "openalex", b),
            ),
            reports=_reports(a, b),
            outcomes=(
                _outcome("title_abstract", a, "include", 2),
                _outcome("title_abstract", b, "exclude", 3),
            ),
            attempts=(_attempt(request, a, "unavailable", 2),),
        )
    )
    counts = body["counts"]
    assert counts["reports_sought"] == 1
    assert counts["reports_not_retrieved"] == 1
    assert counts["records_excluded"] == 1  # b, at title/abstract only
    assert counts["reports_assessed"] == 0
    assert counts["reports_excluded_by_reason"] == {}
    assert all(body["checks"].values())


def test_retry_after_unavailable_counts_retrieved_and_records_amendment() -> None:
    a = uuid4()
    request, first = uuid4(), uuid4()
    retry = _attempt(request, a, "retrieved", 3, previous=first)
    body = derive_prisma_flow(
        _inputs(
            records=(Record("1", "provider", "openalex", a),),
            reports=_reports(a),
            attempts=(_attempt(request, a, "unavailable", 2, attempt_id=first), retry),
            outcomes=(
                _outcome("title_abstract", a, "include", 2),
                _outcome("full_text", a, "exclude", 4, reason="wrong design"),
            ),
        )
    )
    counts = body["counts"]
    assert (counts["reports_sought"], counts["reports_not_retrieved"]) == (1, 0)
    assert counts["reports_assessed"] == 1
    assert counts["reports_excluded_by_reason"] == {"wrong design": 1}
    assert body["amendments"] == [
        {
            "event_id": str(retry.event_id),
            "aggregate_type": "research_acquisition",
            "seq": 3,
            "kind": "acquisition.retrieved",
            "report_id": str(a),
            "from": "unavailable",
            "to": "retrieved",
        }
    ]


def _included(*reports: Report) -> PrismaInputs:
    request_rows = []
    outcomes = []
    for index, report in enumerate(reports):
        request_rows.append(_attempt(uuid4(), report.id, "retrieved", index + 1))
        outcomes.append(_outcome("title_abstract", report.id, "include", index + 1))
        outcomes.append(_outcome("full_text", report.id, "include", index + 1))
    return _inputs(
        records=tuple(
            Record(str(i), "provider", "openalex", r.id) for i, r in enumerate(reports)
        ),
        reports=reports,
        attempts=tuple(request_rows),
        outcomes=tuple(outcomes),
    )


def test_two_reports_one_confirmed_study_counts_one_study() -> None:
    study = uuid4()
    body = derive_prisma_flow(
        _included(
            Report(uuid4(), None, study, "confirmed"),
            Report(uuid4(), None, study, "confirmed"),
            Report(uuid4(), None, None, None),
        )
    )
    counts = body["counts"]
    assert (counts["included_reports"], counts["included_studies"]) == (3, 2)
    assert counts["unconfirmed_study_links"] == 0


def test_proposed_link_counts_separately() -> None:
    study = uuid4()
    body = derive_prisma_flow(
        _included(
            Report(uuid4(), None, study, "confirmed"),
            Report(uuid4(), None, study, "proposed"),
        )
    )
    counts = body["counts"]
    assert (counts["included_studies"], counts["unconfirmed_study_links"]) == (2, 1)


def test_reopened_resolution_uses_current_outcome_and_lists_amendment() -> None:
    a = uuid4()
    reopened = _outcome("title_abstract", a, None, 5, basis="reopened", supersedes=True)
    again = _outcome("title_abstract", a, "exclude", 7, supersedes=True)
    body = derive_prisma_flow(
        _inputs(
            records=(Record("1", "provider", "openalex", a),),
            reports=_reports(a),
            outcomes=(_outcome("title_abstract", a, "include", 3), reopened, again),
        )
    )
    assert body["counts"]["records_screened"] == 1
    assert body["counts"]["records_excluded"] == 1
    # The automatic re-resolution is the reopen's effect, not an amendment.
    assert [(x["kind"], x["from"], x["to"]) for x in body["amendments"]] == [
        ("title_abstract.reopened", "include", "reopened"),
    ]


def test_merge_collision_prefers_survivor_and_warns() -> None:
    a, b = uuid4(), uuid4()
    merge = Merge(uuid4(), 4, a, (b,))
    body = derive_prisma_flow(
        _inputs(
            records=(
                Record("1", "provider", "openalex", a),
                Record("2", "provider", "pubmed", a),
            ),
            reports=(Report(a, None, None, None), Report(b, a, None, None)),
            outcomes=(
                _outcome("title_abstract", a, "include", 2),
                _outcome("title_abstract", b, "exclude", 9),
            ),
            merges=(merge,),
        )
    )
    assert body["counts"]["records_excluded"] == 0
    assert body["warnings"]
    assert [x["kind"] for x in body["amendments"]] == ["identity.report_merged"]


@pytest.mark.parametrize("kind", ["attempt", "outcome", "record"])
def test_duplicate_input_rows_raise(kind: str) -> None:
    a, request = uuid4(), uuid4()
    attempt = _attempt(request, a, "unavailable", 2)
    outcome = _outcome("title_abstract", a, "include", 2)
    record = Record("1", "provider", "openalex", a)
    fields: dict[str, Any] = {
        "records": (record,),
        "reports": _reports(a),
        "attempts": (attempt,),
        "outcomes": (outcome,),
    }
    doubled = {"attempt": "attempts", "outcome": "outcomes", "record": "records"}[kind]
    fields[doubled] = fields[doubled] * 2
    with pytest.raises(PrismaInconsistency):
        derive_prisma_flow(_inputs(**fields))


def test_forked_or_orphan_attempt_chain_raises() -> None:
    a, request, first = uuid4(), uuid4(), uuid4()
    head = _attempt(request, a, "unavailable", 2, attempt_id=first)
    fork = (
        _attempt(request, a, "requested", 3, previous=first),
        _attempt(request, a, "retrieved", 4, previous=first),
    )
    for attempts in ((head, *fork), (_attempt(request, a, "requested", 3, uuid4()),)):
        with pytest.raises(PrismaInconsistency):
            derive_prisma_flow(
                _inputs(
                    records=(Record("1", "provider", "openalex", a),),
                    reports=_reports(a),
                    attempts=attempts,
                )
            )


def test_assessed_without_retrieval_raises() -> None:
    a = uuid4()
    with pytest.raises(PrismaInconsistency):
        derive_prisma_flow(
            _inputs(
                records=(Record("1", "provider", "openalex", a),),
                reports=_reports(a),
                outcomes=(_outcome("full_text", a, "include", 2),),
            )
        )


def test_body_hash_stable_and_moves_with_stream_head() -> None:
    a = uuid4()
    inputs = _inputs(
        records=(Record("1", "provider", "openalex", a),), reports=_reports(a)
    )
    first, second = package(derive_prisma_flow(inputs)), package(
        derive_prisma_flow(inputs)
    )
    assert first["schema"] == "nous.academic.prisma-flow.v1"
    assert first["body_sha256"] == second["body_sha256"]
    assert first["body"]["versions"]["corpus_hash"]
    moved = _inputs(
        records=inputs.records,
        reports=inputs.reports,
        versions={**VERSIONS, "stream_heads": {"research_acquisition:c": 6}},
    )
    assert package(derive_prisma_flow(moved))["body_sha256"] != first["body_sha256"]


def test_markdown_has_mermaid_and_every_count() -> None:
    body = derive_prisma_flow(
        _included(Report(uuid4(), None, None, None), Report(uuid4(), None, None, None))
    )
    markdown = render_markdown(body)
    assert "```mermaid" in markdown and "flowchart TD" in markdown
    for key, value in body["counts"].items():
        assert key in markdown
        if isinstance(value, int):
            assert str(value) in markdown


def test_request_on_report_without_records_is_skipped_with_warning() -> None:
    a, emptied = uuid4(), uuid4()  # e.g. a report whose sources were all split off
    body = derive_prisma_flow(
        _inputs(
            records=(Record("1", "provider", "openalex", a),),
            reports=_reports(a, emptied),
            attempts=(_attempt(uuid4(), emptied, "unavailable", 2),),
        )
    )
    assert (
        body["counts"]["reports_sought"],
        body["counts"]["reports_not_retrieved"],
    ) == (0, 0)
    assert any(str(emptied) in warning for warning in body["warnings"])
