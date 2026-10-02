"""GOO-314 pure peer-review rules: anchors, anchored diff, status."""

import pytest

from src.services.research import peer_review_rules as rules

pytestmark = pytest.mark.unit

V1 = "Alpha holds. Beta rises sharply. Gamma falls."
V2 = "Alpha holds. Beta rises modestly. Gamma falls."


def _anchor(content: str, quote: str) -> tuple[int, int, str]:
    start = content.index(quote)
    end = start + len(quote)
    return start, end, rules.check_anchor(content, start, end, quote)


def test_anchor_exact_on_same_version() -> None:
    start, end, digest = _anchor(V1, "Gamma falls.")
    h1 = rules.sha256(V1)
    assert rules.anchor_state("Gamma falls.", digest, h1, start, end, V1, h1) == (
        "exact",
        start,
        end,
    )
    assert rules.anchor_state(None, None, h1, None, None, V2, rules.sha256(V2)) == (
        "general",
        None,
        None,
    )
    with pytest.raises(ValueError, match="does not match"):
        rules.check_anchor(V1, 0, 5, "Beta ")


def test_anchor_carried_when_quote_found_once_with_new_offsets() -> None:
    target = "Intro. " + V1
    start, end, digest = _anchor(V1, "Gamma falls.")
    state, new_start, new_end = rules.anchor_state(
        "Gamma falls.",
        digest,
        rules.sha256(V1),
        start,
        end,
        target,
        rules.sha256(target),
    )
    assert state == "carried"
    assert (new_start, new_end) == (start + 7, end + 7)
    assert target[new_start:new_end] == "Gamma falls."


def test_anchor_unresolved_when_quote_missing_or_ambiguous() -> None:
    start, end, digest = _anchor(V1, "Beta rises sharply.")
    origin = rules.sha256(V1)
    missing = rules.anchor_state(
        "Beta rises sharply.", digest, origin, start, end, V2, rules.sha256(V2)
    )
    assert missing == ("unresolved_anchor", None, None)
    start, end, digest = _anchor(V1, "Alpha holds.")
    ambiguous = V2 + " Alpha holds."
    assert rules.anchor_state(
        "Alpha holds.", digest, origin, start, end, ambiguous, rules.sha256(ambiguous)
    ) == ("unresolved_anchor", None, None)
    # Overlapping repeats count as ambiguous too.
    assert rules.anchor_state(
        "aa", rules.sha256("aa"), origin, 0, 2, "aaa", rules.sha256("aaa")
    )[0] == ("unresolved_anchor")


def test_diff_reports_offsets_on_both_sides_for_replace_insert_delete() -> None:
    old = "One. Two. Three."
    new = "One. Deux. Three. Four."
    hunks = rules.anchored_diff(old, new)
    assert [h["op"] for h in hunks] == ["replace", "insert"]
    replace, insert = hunks
    assert old[slice(*replace["old"])] == replace["old_text"] == "Two."
    assert new[slice(*replace["new"])] == replace["new_text"] == "Deux."
    assert insert["old"][0] == insert["old"][1] == len(old)
    assert insert["new_text"] == "Four."
    deleted = rules.anchored_diff(old, "One. Three.")
    assert [h["op"] for h in deleted] == ["delete"]
    assert deleted[0]["old_text"] == "Two."
    assert deleted[0]["new"][0] == deleted[0]["new"][1] == len("One.")


def test_diff_empty_for_identical_content() -> None:
    assert rules.anchored_diff(V1, V1) == []
    assert rules.diff_sha256([]) == rules.diff_sha256([])


def test_touches_requires_overlap_with_anchor() -> None:
    hunks = rules.anchored_diff(V1, V2)
    beta = V1.index("Beta")
    gamma = V1.index("Gamma")
    assert rules.touches(hunks, beta, beta + len("Beta rises sharply."))
    assert not rules.touches(hunks, gamma, len(V1))
    assert not rules.touches(hunks, 0, len("Alpha holds."))
    insert = [{"old": [12, 12]}]
    assert rules.touches(insert, 0, 12)
    assert not rules.touches(insert, 13, 20)


def test_status_resolution_of_older_response_is_responded() -> None:
    assert rules.comment_status(None, None) == "open"
    assert rules.comment_status("r2", None) == "responded"
    assert rules.comment_status("r2", ("resolved", "r1")) == "responded"
    assert rules.comment_status("r2", ("resolved", "r2")) == "resolved"
    assert rules.comment_status("r2", ("reopened", "r2")) == "responded"


def test_unicode_offsets_are_python_str_indices() -> None:
    content = "Ünïcode 🧪 first. Second 𝔘 claim."
    quote = "Second 𝔘 claim."
    start, end, digest = _anchor(content, quote)
    assert content[start:end] == quote
    assert digest == rules.sha256(quote)
    revised = "Ünïcode 🧪 first. Second 𝔘 claim, revised."
    hunk = rules.anchored_diff(content, revised)[0]
    assert content[slice(*hunk["old"])] == quote
    assert revised[slice(*hunk["new"])] == "Second 𝔘 claim, revised."


def test_response_shape_mirrors_checks() -> None:
    rules.check_response_shape("change", "d", None)
    rules.check_response_shape("no_change", None, "Out of scope.")
    for kind, revised, rationale in (
        ("change", None, None),
        ("change", "d", "why"),
        ("no_change", None, "  "),
        ("no_change", "d", "why"),
        ("other", None, "why"),
    ):
        with pytest.raises(ValueError):
            rules.check_response_shape(kind, revised, rationale)
