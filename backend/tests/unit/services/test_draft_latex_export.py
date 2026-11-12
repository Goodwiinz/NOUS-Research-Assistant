"""LaTeX draft export keeps section headings and escaping (RC-1, R6-L8).

``_convert_to_latex`` escaped the whole markdown before converting headers,
so ``## Title`` became ``\\#\\# Title`` and no ``\\section`` was ever emitted.
Headings must convert while user text (titles included) stays escaped.
"""

from __future__ import annotations

import pytest

from src.services.research.draft_generation_service import DraftGenerationService

pytestmark = pytest.mark.unit


def _body(markdown: str) -> str:
    service = DraftGenerationService.__new__(DraftGenerationService)
    latex = service._convert_to_latex(markdown)
    start = latex.index("\\maketitle\n") + len("\\maketitle\n")
    end = latex.index("\\bibliographystyle")
    return latex[start:end].strip("\n")


def test_numbered_h2_becomes_section() -> None:
    assert _body("## 1. Intro") == "\\section{Intro}"


def test_h2_without_number_becomes_section() -> None:
    assert _body("## Methods") == "\\section{Methods}"


def test_h3_becomes_subsection() -> None:
    assert _body("### Sub") == "\\subsection{Sub}"


def test_heading_text_is_escaped() -> None:
    body = _body("## 2. A \\input{x} 50% #1 a_b & $y$")
    assert body == "\\section{A \\\\input\\{x\\} 50\\% \\#1 a\\_b \\& \\$y\\$}"


def test_body_line_is_escaped() -> None:
    body = _body("Text \\cmd{a} 5% #x a_b & $z$")
    assert body == "Text \\\\cmd\\{a\\} 5\\% \\#x a\\_b \\& \\$z\\$"


def test_doc_marker_becomes_cite() -> None:
    assert _body("Alpha holds [Doc 3].") == "Alpha holds \\cite{doc3}."


def test_h1_left_as_escaped_text() -> None:
    assert _body("# Top") == "\\# Top"


def test_mixed_document() -> None:
    md = "## 1. Intro\nSee [Doc 1].\n\n### Detail\nCost is 5%.\n"
    assert _body(md) == (
        "\\section{Intro}\nSee \\cite{doc1}.\n\n\\subsection{Detail}\nCost is 5\\%."
    )


def test_title_is_escaped_first_line() -> None:
    service = DraftGenerationService.__new__(DraftGenerationService)
    latex = service._convert_to_latex("## Results & 50%\nbody")
    assert "\\title{Results \\& 50\\%}" in latex
