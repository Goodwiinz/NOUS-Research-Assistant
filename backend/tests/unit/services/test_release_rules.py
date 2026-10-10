"""Pure release gate and dependency walk (GOO-307).

Mutation verification (``docs/testing/agent-orchestration-mutation-checks.md``,
GOO-307 section): ``dependents`` returning every reachable-or-not release
fails ``-k selective``.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from src.services.research import release_rules as rr

pytestmark = pytest.mark.unit

SENTENCE_A = "Mortality fell by 12% [Doc 1]."
SENTENCE_B = "The effect remains uncertain [Doc 2]."
CONTENT = f"## Results\n{SENTENCE_A} {SENTENCE_B}\n"


def _span(text: str) -> tuple[int, int]:
    start = CONTENT.index(text)
    return start, start + len(text)


def _link(
    kind: str = "source_span", live: bool = True, observed: bool = False
) -> rr.LinkIn:
    return rr.LinkIn(id=uuid4(), kind=kind, live=live, observed=observed)


def _claim(
    text: str,
    links: tuple[rr.LinkIn, ...] = (),
    stance: str | None = None,
    cited: tuple[UUID, ...] | None = None,
    kind: str = "factual",
    attributed: str | None = None,
    is_tip: bool = True,
) -> rr.ClaimIn:
    start, end = _span(text)
    assessment = None
    if stance is not None:
        assessment = rr.AssessmentIn(
            id=uuid4(),
            stance=stance,
            link_ids=tuple(link.id for link in links) if cited is None else cited,
        )
    return rr.ClaimIn(
        claim_version_id=uuid4(),
        kind=kind,
        start=start,
        end=end,
        text=text,
        attributed_to=attributed,
        links=links,
        assessment=assessment,
        is_tip=is_tip,
    )


def _supported(text: str) -> rr.ClaimIn:
    return _claim(text, (_link(),), "supporting")


def _codes(gate: rr.GateResult) -> list[str]:
    return [b.code for b in gate.blockers]


def test_every_assertion_span_must_be_claimed() -> None:
    gate = rr.check_release(CONTENT, [_supported(SENTENCE_A)], {}, set())
    assert _codes(gate) == ["unclaimed_assertion"]
    (blocker,) = gate.blockers
    assert CONTENT[blocker.start : blocker.end] == SENTENCE_B == blocker.text
    assert blocker.claim_version_id is None
    assert rr.assertion_spans("## Heading\n\n") == []
    ok = rr.check_release(
        CONTENT, [_supported(SENTENCE_A), _supported(SENTENCE_B)], {}, set()
    )
    assert ok.blockers == ()


def test_model_only_stance_observation_blocks_assessed_supporting_passes() -> None:
    observed = _link(observed=True)
    model_only = _claim(SENTENCE_A, (observed,))
    gate = rr.check_release(CONTENT, [model_only, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["model_only"]
    assert gate.blockers[0].claim_version_id == model_only.claim_version_id
    unassessed = _claim(SENTENCE_A, (_link(),))
    gate = rr.check_release(CONTENT, [unassessed, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["unassessed"]
    assessed = _claim(SENTENCE_A, (observed,), "supporting")
    gate = rr.check_release(CONTENT, [assessed, _supported(SENTENCE_B)], {}, set())
    assert gate.blockers == ()
    assert set(gate.claim_version_ids) == {
        assessed.claim_version_id,
        gate.claim_version_ids[1],
    }
    assert assessed.assessment is not None
    assert assessed.assessment.id in gate.assessment_ids


def test_supported_statement_of_uncertainty_passes() -> None:
    uncertain = _claim(SENTENCE_B, (_link("extraction"),), "supporting")
    gate = rr.check_release(CONTENT, [_supported(SENTENCE_A), uncertain], {}, set())
    assert gate.blockers == ()
    assert gate.dimensions["support"]["status"] == "passed"


def test_legacy_unanchored_links_only_block_as_legacy_only() -> None:
    legacy = _claim(SENTENCE_A, (_link("legacy_unanchored"),))
    gate = rr.check_release(CONTENT, [legacy, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["legacy_only"]
    cited_legacy = _claim(SENTENCE_A, (_link("legacy_unanchored"),), "supporting")
    gate = rr.check_release(CONTENT, [cited_legacy, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["legacy_only"]


def test_withdrawn_or_superseded_link_in_assessment_blocks() -> None:
    dead = _link(live=False)
    claim = _claim(SENTENCE_A, (dead,), "supporting")
    gate = rr.check_release(CONTENT, [claim, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["stale_evidence"]
    missing = _claim(SENTENCE_A, (_link(),), "supporting", cited=(uuid4(),))
    gate = rr.check_release(CONTENT, [missing, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["stale_evidence"]
    live = _link()
    stale = _claim(SENTENCE_A, (live,), "supporting")
    gate = rr.check_release(
        CONTENT, [stale, _supported(SENTENCE_B)], {}, {rr.node("link", live.id)}
    )
    assert _codes(gate) == ["stale_evidence"]
    moved = _claim(SENTENCE_A, (_link(),), "supporting", is_tip=False)
    gate = rr.check_release(CONTENT, [moved, _supported(SENTENCE_B)], {}, set())
    assert _codes(gate) == ["superseded_assessment"]


@pytest.mark.parametrize(
    ("stance", "code"),
    [
        ("opposing", "opposed"),
        ("neutral", "unresolved"),
        ("not_addressed", "unresolved"),
        ("unresolved", "unresolved"),
    ],
)
def test_unresolved_and_opposed_block_with_offsets(stance: str, code: str) -> None:
    claim = _claim(SENTENCE_B, (_link(),), stance)
    gate = rr.check_release(CONTENT, [_supported(SENTENCE_A), claim], {}, set())
    (blocker,) = gate.blockers
    assert blocker.code == code
    assert (blocker.start, blocker.end) == _span(SENTENCE_B)
    assert blocker.text == SENTENCE_B


def test_interpretation_exempt_but_must_be_attributed() -> None:
    bare = _claim(SENTENCE_B, kind="interpretation")
    gate = rr.check_release(CONTENT, [_supported(SENTENCE_A), bare], {}, set())
    assert _codes(gate) == ["unattributed_interpretation"]
    signed = _claim(SENTENCE_B, kind="interpretation", attributed="J. Adjudicator")
    gate = rr.check_release(CONTENT, [_supported(SENTENCE_A), signed], {}, set())
    assert gate.blockers == ()
    assert gate.interpretation_ids == (signed.claim_version_id,)
    assert signed.claim_version_id not in gate.claim_version_ids


def test_superseded_interpretation_blocks() -> None:
    """A re-versioned interpretation is no longer the claim of record: passing
    it mints a release that is derived-stale the moment it exists."""
    moved = _claim(
        SENTENCE_B, kind="interpretation", attributed="J. Adjudicator", is_tip=False
    )
    gate = rr.check_release(CONTENT, [_supported(SENTENCE_A), moved], {}, set())
    assert _codes(gate) == ["superseded_claim"]
    assert gate.blockers[0].claim_version_id == moved.claim_version_id


def _review(identity: str, publication: str = "unknown") -> dict:
    return {
        "fully_verified": True,
        "verdicts": [
            {
                "doc_index": 1,
                "document_id": str(uuid4()),
                "checks": {
                    "identity": {"status": identity},
                    "publication": {"observation_status": publication},
                },
            }
        ],
    }


def test_identity_mismatch_blocks_publication_unknown_reported_not_blocking() -> None:
    claims = [_supported(SENTENCE_A), _supported(SENTENCE_B)]
    gate = rr.check_release(CONTENT, claims, _review("mismatch"), set())
    assert _codes(gate) == ["identity_mismatch"]
    assert CONTENT[gate.blockers[0].start : gate.blockers[0].end] == "[Doc 1]"
    gate = rr.check_release(CONTENT, claims, _review("unresolved"), set())
    assert gate.blockers == ()
    assert gate.dimensions["identity"]["reported"] == [
        {"doc_index": 1, "status": "unresolved"}
    ]
    assert gate.dimensions["publication"]["status"] == "unavailable"
    gate = rr.check_release(CONTENT, claims, _review("match", "retracted"), set())
    assert _codes(gate) == ["retracted_source"]
    gate = rr.check_release(CONTENT, claims, _review("match", "corrected"), set())
    assert gate.blockers == ()


def test_fully_verified_flag_is_ignored() -> None:
    unassessed = _claim(SENTENCE_A, (_link(),))
    gate = rr.check_release(
        CONTENT, [unassessed, _supported(SENTENCE_B)], _review("match"), set()
    )
    assert _codes(gate) == ["unassessed"]


def _graph() -> tuple[list[tuple[rr.Node, rr.Node]], dict[str, rr.Node]]:
    """Two claims on different accepted values of different sources."""
    n = {
        "d1": rr.source_node(uuid4(), "a" * 64, "b" * 64),
        "d2": rr.source_node(uuid4(), "c" * 64, "d" * 64),
        "acc1": rr.node("accepted", uuid4()),
        "acc2": rr.node("accepted", uuid4()),
        "link1": rr.node("link", uuid4()),
        "link2": rr.node("link", uuid4()),
        "as1": rr.node("assessment", uuid4()),
        "as2": rr.node("assessment", uuid4()),
        "cv1": rr.node("claim_version", uuid4()),
        "cv2": rr.node("claim_version", uuid4()),
        "rel1": rr.node("release", uuid4()),
        "rel2": rr.node("release", uuid4()),
    }
    edges = [
        (n["d1"], n["acc1"]),
        (n["d2"], n["acc2"]),
        (n["acc1"], n["link1"]),
        (n["acc2"], n["link2"]),
        (n["link1"], n["as1"]),
        (n["link2"], n["as2"]),
        (n["as1"], n["rel1"]),
        (n["cv1"], n["rel1"]),
        (n["as2"], n["rel2"]),
        (n["cv2"], n["rel2"]),
    ]
    return edges, n


def test_dependents_walk_is_selective() -> None:
    edges, n = _graph()
    reached = rr.dependents(edges, {n["d1"]})
    assert {n["acc1"], n["link1"], n["as1"], n["rel1"]} <= reached
    assert not reached & {n["acc2"], n["link2"], n["as2"], n["rel2"], n["cv2"]}
    assert rr.dependents(edges, set()) == set()


def test_dependents_form_change_reaches_only_changed_field() -> None:
    edges, n = _graph()
    assert rr.dependents(edges, {n["acc2"]}) == {n["link2"], n["as2"], n["rel2"]}
    assert rr.dependents(edges, {n["cv1"]}) == {n["rel1"]}


def test_status_candidate_verified_stale_and_repromoted() -> None:
    live = SimpleNamespace(stale_at=None)
    stale = SimpleNamespace(stale_at="2026-09-30")
    assert rr.release_status([], False) == "candidate"
    assert rr.release_status([live], False) == "verified"
    assert rr.release_status([live], True) == "stale"
    assert rr.release_status([stale], False) == "stale"
    assert rr.release_status([stale, live], False) == "verified"


def test_label_export_keeps_content_bytes_and_marks_unresolved() -> None:
    model_only = _claim(SENTENCE_A, (_link(observed=True),))
    interpretation = _claim(SENTENCE_B, kind="interpretation", attributed="J. Doe")
    gate = rr.check_release(CONTENT, [model_only, interpretation], {}, set())
    original = str(CONTENT)
    header = rr.status_header("candidate", gate)
    out = rr.label_export(CONTENT, gate, "markdown", header)
    assert CONTENT == original
    assert out.startswith("> Status: CANDIDATE — not verified. 1 unresolved item(s).")
    assert f"{SENTENCE_A} **[UNRESOLVED: model_only]**" in out
    assert f"{SENTENCE_B} *[Interpretation — J. Doe]*" in out
    assert "## Unresolved items" in out
    stripped = (
        out.split("\n\n", 1)[1]
        .split("\n\n## Unresolved items", 1)[0]
        .replace(" **[UNRESOLVED: model_only]**", "")
        .replace(" *[Interpretation — J. Doe]*", "")
    )
    assert stripped == CONTENT
    latex = rr.label_export(CONTENT, gate, "latex", header)
    assert latex.split("\n", 1)[0] == CONTENT.split("\n", 1)[0]  # title line kept
    assert "[UNRESOLVED: model_only]" in latex and "**" not in latex
    verified = rr.status_header(
        "verified", gate, release_id="r-1", content_hash="f" * 64
    )
    assert verified == "> Status: VERIFIED release r-1 · sha256 ffffffffffff"
    assert rr.status_header("stale", gate, cause="research_extraction").startswith(
        "> Status: STALE — invalidated by research_extraction"
    )
