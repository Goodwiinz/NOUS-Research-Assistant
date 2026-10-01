"""Tests for the Extraction Matrix service."""

import pytest

from src.services.research.extraction_matrix_service import ExtractionMatrixService


def test_build_extraction_prompt():
    service = ExtractionMatrixService()
    columns = [
        {"name": "Methodology", "description": "Research methodology used"},
        {"name": "Sample Size", "description": "Number of participants"},
    ]
    prompt = service._build_extraction_prompt(
        columns, "This study used a randomized trial with 500 participants."
    )
    assert "Methodology" in prompt
    assert "Sample Size" in prompt
    assert "randomized trial" in prompt


def test_parse_extraction_result_valid_json():
    service = ExtractionMatrixService()
    columns = [{"name": "Methodology"}, {"name": "Sample Size"}]
    raw = '{"Methodology": {"value": "RCT", "citation": "p.3"}, "Sample Size": {"value": "500", "citation": "p.5"}}'
    result = service._parse_extraction_result(raw, columns)
    assert result["Methodology"]["value"] == "RCT"
    assert result["Sample Size"]["value"] == "500"


def test_parse_extraction_result_missing_column():
    service = ExtractionMatrixService()
    columns = [{"name": "Methodology"}, {"name": "Missing"}]
    raw = '{"Methodology": {"value": "RCT"}}'
    result = service._parse_extraction_result(raw, columns)
    assert result["Methodology"]["value"] == "RCT"
    assert result["Missing"]["value"] is None
    # GOO-304: an absent column is recorded as an extraction error, not a blank.
    assert result["Missing"]["missing"] == "extraction_error"
    assert result["Methodology"]["missing"] is None


def test_parse_extraction_result_invalid_json():
    """Gracefully handle invalid JSON from LLM output."""
    service = ExtractionMatrixService()
    columns = [{"name": "Methodology"}]
    raw = "This is not valid JSON at all"
    result = service._parse_extraction_result(raw, columns)
    # All columns should be filled with None values
    assert result["Methodology"]["value"] is None
    assert result["Methodology"]["citation"] is None
    assert result["Methodology"]["missing"] == "extraction_error"


def test_parse_extraction_result_reported_missingness():
    """GOO-304: only not_reported/not_applicable are machine-declared reasons."""
    service = ExtractionMatrixService()
    columns = [{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]
    raw = (
        '{"A": {"missing": "not_reported"}, "B": {"value": null, "missing": null},'
        ' "C": {"value": "x", "missing": "not_applicable"},'
        ' "D": {"missing": "unresolved_disagreement"}}'
    )
    result = service._parse_extraction_result(raw, columns)
    assert result["A"] == {"value": None, "missing": "not_reported", "citation": None}
    for name in ("B", "C", "D"):
        assert result[name]["missing"] == "extraction_error"
        assert result[name]["value"] is None


def test_build_extraction_prompt_no_description():
    """Columns without descriptions should still appear in the prompt."""
    service = ExtractionMatrixService()
    columns = [{"name": "Findings"}]
    prompt = service._build_extraction_prompt(columns, "Some document text.")
    assert "Findings" in prompt
    assert "Some document text." in prompt
