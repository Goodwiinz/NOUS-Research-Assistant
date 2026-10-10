"""Regression: text extraction fails loudly instead of indexing an error string.

extract_text_content / _extract_text_from_path previously returned
"Error extracting text: ..." (or "Spreadsheet processing not available") on
failure, which the pipeline stored as document.content_text and marked the doc
COMPLETED/indexed. Failures must raise (so the doc is failed); capability gaps
return "" (empty), never a fabricated content string.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.models.document import DocumentType
from src.services.documents import file_service as fs_mod
from src.services.documents.file_service import FileService


def _svc():
    return FileService(db=None)


@pytest.mark.unit
def test_text_extraction_failure_raises_not_error_string():
    svc = _svc()
    doc = SimpleNamespace(id="d1", document_type=DocumentType.TEXT)
    # Nonexistent path → open() raises → must propagate, not return a string.
    with pytest.raises(Exception):
        svc._extract_text_from_path("/no/such/file.txt", doc)


@pytest.mark.unit
def test_spreadsheet_capability_gap_returns_empty_not_string():
    svc = _svc()
    doc = SimpleNamespace(id="d2", document_type=DocumentType.SPREADSHEET)
    with patch.object(fs_mod, "PANDAS_AVAILABLE", False):
        result = svc._extract_text_from_path("/whatever.xlsx", doc)
    assert result == ""
    assert "not available" not in result


@pytest.mark.unit
def test_spreadsheet_parse_failure_raises():
    """pandas IS available but the file is corrupt → must raise (FAILED), not
    return "" (which would index the corrupt spreadsheet as empty)."""
    svc = _svc()
    doc = SimpleNamespace(id="d3", document_type=DocumentType.SPREADSHEET)
    fake_pd = MagicMock()
    fake_pd.read_excel.side_effect = ValueError("corrupt xlsx")
    with (
        patch.object(fs_mod, "PANDAS_AVAILABLE", True),
        patch.object(fs_mod, "pd", fake_pd),
    ):
        with pytest.raises(Exception):
            svc._extract_text_from_path("/corrupt.xlsx", doc)


@pytest.mark.unit
def test_pdf_extraction_adds_stable_page_markers():
    doc = SimpleNamespace(id="pdf-1", document_type=DocumentType.PDF)
    reader = MagicMock()
    reader.pages = [
        MagicMock(extract_text=MagicMock(return_value="first")),
        MagicMock(extract_text=MagicMock(return_value="second")),
    ]
    with (
        patch("builtins.open", MagicMock()),
        patch.object(fs_mod, "PdfReader", return_value=reader),
    ):
        result = _svc()._extract_text_from_path("/paper.pdf", doc)

    assert result == "[Page 1]\nfirst\n\n[Page 2]\nsecond"


@pytest.mark.unit
def test_presentation_corrupt_raises(monkeypatch):
    """A corrupt presentation (ValueError/IOError) must raise → FAILED."""
    fake_pptx = types.ModuleType("pptx")

    def _boom(path):
        raise ValueError("corrupt pptx")

    fake_pptx.Presentation = _boom
    monkeypatch.setitem(sys.modules, "pptx", fake_pptx)

    svc = _svc()
    doc = SimpleNamespace(id="d4", document_type=DocumentType.PRESENTATION)
    with pytest.raises(Exception):
        svc._extract_text_from_path("/corrupt.pptx", doc)


@pytest.mark.unit
def test_presentation_missing_dep_returns_empty(monkeypatch):
    """python-pptx not installed is a capability gap, not a bad file → ""
    (don't fail the document over a missing optional dependency)."""
    monkeypatch.setitem(sys.modules, "pptx", None)  # `import pptx` → ImportError

    svc = _svc()
    doc = SimpleNamespace(id="d5", document_type=DocumentType.PRESENTATION)
    assert svc._extract_text_from_path("/x.pptx", doc) == ""
