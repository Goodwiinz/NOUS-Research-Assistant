"""Pure claim rules (GOO-306): passage anchors, link shapes, assessments."""

import hashlib
from uuid import uuid4

import pytest

from src.api.evidence.router import consensus_calculator
from src.services.research import claim_rules as rules

pytestmark = pytest.mark.unit

H1, H2 = "a" * 64, "b" * 64


def test_normalized_hash_equals_stance_claim_hash() -> None:
    # The join key into stance_classifications.claim_hash: the meter hashes the
    # claim with this exact calculator (api/evidence/router.py).
    text = "  Aspirin   REDUCES stroke risk. "
    assert rules.normalized_hash(text) == consensus_calculator._generate_claim_hash(
        text
    )
    assert rules.normalized_hash(text) == rules.normalized_hash(
        "aspirin reduces stroke risk."
    )


def test_content_hash_is_draft_review_recipe() -> None:
    content = "naïve 🧪 trial"
    assert (
        rules.content_hash(content)
        == hashlib.sha256(content.encode("utf-8")).hexdigest()
    )


def test_passage_must_equal_exact_slice_code_points() -> None:
    content = "A naïve 🧪 trial showed benefit."
    start = content.index("🧪")
    rules.check_passage(content, start, start + len("🧪 trial"), "🧪 trial")
    with pytest.raises(ValueError, match="Text does not match the draft passage"):
        rules.check_passage(content, start, start + len("🧪 trial"), "🧪 Trial")
    # UTF-8 byte offsets are not code points.
    byte_start = len(content[:start].encode("utf-8"))
    with pytest.raises(ValueError):
        rules.check_passage(content, byte_start, byte_start + 7, "🧪 trial")


def test_passage_out_of_range_or_blank_rejected() -> None:
    content = "short text"
    for start, end in ((-1, 3), (3, 3), (4, 2), (0, len(content) + 1)):
        with pytest.raises(ValueError):
            rules.check_passage(content, start, end, content[max(start, 0) : end])
    with pytest.raises(ValueError, match="blank"):
        rules.check_passage("a   b", 1, 4, "   ")


def _cols(**overrides: object) -> dict:
    base: dict = {
        "accepted_value_id": None,
        "draft_citation_id": None,
        "document_id": None,
        "source_hash": None,
        "text_sha256": None,
        "start_char": None,
        "end_char": None,
        "quote": None,
        "status": "linked",
        "supersedes_link_id": None,
    }
    return base | overrides


def test_link_shape_per_kind() -> None:
    doc = uuid4()
    rules.check_link_shape(
        "extraction",
        **_cols(accepted_value_id=uuid4(), document_id=doc, source_hash=H1),
    )
    rules.check_link_shape(
        "source_span",
        **_cols(
            document_id=doc,
            source_hash=H1,
            text_sha256=H2,
            start_char=0,
            end_char=4,
            quote="abcd",
        ),
    )
    rules.check_link_shape("legacy_unanchored", **_cols(draft_citation_id=uuid4()))
    rejected = [
        # A legacy link never carries a hash or a span.
        ("legacy_unanchored", _cols(draft_citation_id=uuid4(), source_hash=H1)),
        ("legacy_unanchored", _cols(draft_citation_id=uuid4(), start_char=0)),
        ("legacy_unanchored", _cols()),
        # source_span needs a quote and a real span.
        (
            "source_span",
            _cols(
                document_id=doc,
                source_hash=H1,
                text_sha256=H2,
                start_char=0,
                end_char=4,
            ),
        ),
        (
            "source_span",
            _cols(
                document_id=doc,
                source_hash=H1,
                text_sha256=H2,
                start_char=4,
                end_char=4,
                quote="x",
            ),
        ),
        # extraction carries no span of its own.
        (
            "extraction",
            _cols(
                accepted_value_id=uuid4(), document_id=doc, source_hash=H1, quote="q"
            ),
        ),
        ("extraction", _cols(document_id=doc, source_hash=H1)),
        ("nonsense", _cols()),
        # A withdrawal supersedes something.
        ("legacy_unanchored", _cols(draft_citation_id=uuid4(), status="withdrawn")),
        ("legacy_unanchored", _cols(draft_citation_id=uuid4(), status="gone")),
    ]
    for kind, cols in rejected:
        with pytest.raises(ValueError):
            rules.check_link_shape(kind, **cols)


def test_assessment_requires_live_links_unless_unresolved() -> None:
    live = [uuid4(), uuid4()]
    rules.check_assessment("supporting", [live[0]], [], live)
    for stance in ("unresolved", "not_addressed"):
        rules.check_assessment(stance, [], [], live)
    with pytest.raises(ValueError, match="must cite"):
        rules.check_assessment("supporting", [], [], live)
    with pytest.raises(ValueError, match="live link"):
        rules.check_assessment("opposing", [uuid4()], [], live)
    with pytest.raises(ValueError, match="unique"):
        rules.check_assessment("neutral", [live[0], live[0]], [], live)
    with pytest.raises(ValueError, match="Stance"):
        rules.check_assessment("supports", [live[0]], [], live)


def test_observation_must_belong_to_cited_link() -> None:
    live = [uuid4(), uuid4()]
    rules.check_assessment("supporting", [live[0]], [live[0]], live)
    with pytest.raises(ValueError, match="cited link"):
        rules.check_assessment("supporting", [live[0]], [live[1]], live)


def test_link_source_changed() -> None:
    assert not rules.link_source_changed(H1, H1, True, False)
    assert rules.link_source_changed(H1, H2, True, False)
    assert rules.link_source_changed(H1, None, True, False)
    assert rules.link_source_changed(H1, H1, False, False)
    assert rules.link_source_changed(H1, H1, True, True)
    # A legacy link pins nothing, so it never reads changed.
    assert not rules.link_source_changed(None, H2, True, False)
