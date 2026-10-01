"""GOO-305 pure source-anchor verification."""

import hashlib

import pytest

from src.services.research.source_anchors import (
    context,
    page_at,
    text_sha256,
    verify_anchor,
)

pytestmark = pytest.mark.unit

TEXT = "Intro. We enrolled 120 participants. Methods follow. Results were mixed."


def test_unique_quote_verified_with_global_offsets() -> None:
    quote = "We enrolled 120 participants."
    anchor = verify_anchor(TEXT, quote)
    assert anchor.status == "verified"
    assert anchor.start_char == TEXT.index(quote)
    assert anchor.end_char == anchor.start_char + len(quote)
    assert TEXT[anchor.start_char : anchor.end_char] == quote
    assert anchor.occurrences_in_text == 1


def test_repeated_quote_is_ambiguous_and_lists_occurrences() -> None:
    text = "The trial was randomized. Later, the trial was randomized again."
    anchor = verify_anchor(text, "the trial was randomized")
    assert anchor.status == "verified"  # case-sensitive: "The" differs
    anchor = verify_anchor(text, "trial was randomized")
    assert anchor.status == "ambiguous"
    assert anchor.start_char is None and anchor.end_char is None
    assert anchor.occurrences == (4, 37)
    assert anchor.occurrences_in_text == 2


def test_start_hint_disambiguates() -> None:
    text = "trial was randomized; trial was randomized"
    anchor = verify_anchor(text, "trial was randomized", start_hint=22)
    assert (anchor.status, anchor.start_char, anchor.end_char) == ("verified", 22, 42)
    assert anchor.occurrences_in_text == 2


def test_wrong_start_hint_is_unverified_not_researched() -> None:
    anchor = verify_anchor(TEXT, "We enrolled 120 participants.", start_hint=0)
    assert anchor.status == "unverified"
    assert anchor.start_char is None


def test_ocr_noise_quote_not_found_is_unverified() -> None:
    text = "Participants were randomized to two arms."
    anchor = verify_anchor(text, "were random1zed to two arms")
    assert anchor.status == "unverified"
    assert anchor.start_char is None and anchor.occurrences_in_text == 0
    assert verify_anchor(text, None).status == "unverified"
    assert verify_anchor(text, "   ").status == "unverified"


def test_no_text_is_location_unavailable() -> None:
    assert verify_anchor(None, "x").status == "location_unavailable"
    assert verify_anchor("", "x").status == "location_unavailable"


def test_page_from_markers_and_none_without_markers() -> None:
    text = "[Page 1]\nAbstract here.\n[Page 2]\nWe enrolled 40 adults.\n[Page 3]\nEnd."
    anchor = verify_anchor(text, "We enrolled 40 adults.")
    assert anchor.page == 2
    assert page_at(text, 0) == 1
    assert page_at(text, len(text) - 1) == 3
    assert verify_anchor(TEXT, "Methods follow.").page is None


def test_offsets_are_code_points() -> None:
    text = "A naïve 🧪 trial enrolled 9."
    quote = "trial enrolled 9"
    anchor = verify_anchor(text, quote)
    assert anchor.start_char == 10  # code points, not UTF-16 units or bytes
    assert anchor.end_char is not None
    assert text[anchor.start_char : anchor.end_char] == quote
    before, after = context(text, 10, anchor.end_char, radius=2)
    assert (before, after) == ("🧪 ", ".")


def test_unique_in_window_repeated_in_text_is_verified_with_count() -> None:
    text = "n=12 was reported. " + "x" * 100 + " n=12 was reported."
    anchor = verify_anchor(text, "n=12", window=(0, 50))
    assert (anchor.status, anchor.start_char) == ("verified", 0)
    assert anchor.occurrences_in_text == 2
    # A quote straddling the window end is not "in" the window.
    assert verify_anchor(text, "n=12 was", window=(0, 5)).status == "unverified"


def test_changed_source_changes_text_sha256() -> None:
    checksum = hashlib.sha256(b"%PDF-1.7 file bytes").hexdigest()
    before = "Body text."
    after = before + "\n\nFIGURE 1: caption merged on reprocessing."
    assert checksum == hashlib.sha256(b"%PDF-1.7 file bytes").hexdigest()
    assert text_sha256(before) != text_sha256(after)
    assert text_sha256(before) == hashlib.sha256(before.encode("utf-8")).hexdigest()
    assert text_sha256(None) is None
