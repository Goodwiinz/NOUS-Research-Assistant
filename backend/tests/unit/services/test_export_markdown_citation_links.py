"""Markdown thread exports keep citation provenance usable after download."""

from __future__ import annotations

from datetime import datetime

import pytest
from markdown_it import MarkdownIt

from src.core.config import settings
from src.services.research.export_service import MarkdownFormatter
from src.shared.export_schemas import (
    CitationExport,
    ExportFormat,
    ExportOptions,
    MessageExport,
    ThreadExport,
)

pytestmark = pytest.mark.unit

_EXPORTED_AT = datetime(2026, 9, 14, 12, 0, 0)


def _render(citation: CitationExport) -> str:
    thread = ThreadExport(
        id="thread-1",
        title="Research",
        status="active",
        created_at=_EXPORTED_AT,
        updated_at=_EXPORTED_AT,
        last_message_at=_EXPORTED_AT,
        message_count=1,
        token_count=10,
        conversation_id="conversation-1",
        messages=[
            MessageExport(
                id="message-1",
                role="assistant",
                content="Answer with evidence.",
                created_at=_EXPORTED_AT,
                citations=[citation],
            )
        ],
        export_format=ExportFormat.MARKDOWN,
    )
    return MarkdownFormatter().format(thread, ExportOptions()).decode("utf-8")


def test_internal_citation_uses_configured_absolute_frontend_link_and_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the configured origin, internal destination, or page label fails."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech/")
    monkeypatch.setattr(
        settings,
        "CORS_ORIGINS",
        "https://dev-app.gen-text.app,http://localhost:3000",
    )

    markdown = _render(
        CitationExport(
            id="citation-1",
            document_id="123e4567-e89b-12d3-a456-426614174000",
            document_title="Evidence [draft] *reviewed*",
            snippet="The supporting excerpt stays in the export.",
            page_number=7,
            score=0.91,
        )
    )

    assert (
        "- **[Evidence \\[draft\\] \\*reviewed\\*]"
        "(https://goodwiinz.tech/documents/123e4567-e89b-12d3-a456-426614174000)**"
        " (p. 7) [0.91]" in markdown
    )
    assert "  > The supporting excerpt stays in the export." in markdown
    assert "dev-app.gen-text.app/documents" not in markdown
    assert "localhost:3000/documents" not in markdown


def test_safe_external_url_is_preserved_with_markdown_safe_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing URL preservation or destination escaping fails this assertion."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(
            id="citation-1",
            external_reference_id=(
                "https://example.org/papers/result_(final).pdf#section-2"
            ),
            document_title="Result",
        )
    )

    assert (
        "**[Result](https://example.org/papers/result_%28final%29.pdf#section-2)**"
        in markdown
    )


@pytest.mark.parametrize("document_id", ["doc-1", None])
def test_citation_titles_are_html_escaped_without_corrupting_markdown(
    monkeypatch: pytest.MonkeyPatch,
    document_id: str | None,
) -> None:
    """Bypassing autoescape or double-escaping angle brackets breaks this output."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(
            id="citation-1",
            document_id=document_id,
            document_title="Evidence <img src=x onerror=alert(1)> & [draft]",
        )
    )
    source = markdown.split("### Sources", 1)[1].split("---", 1)[0]

    assert "<" not in source
    assert ">" not in source
    rendered = MarkdownIt("commonmark").render(source)
    assert "Evidence &lt;img src=x onerror=alert(1)&gt; &amp; [draft]" in rendered
    assert "<img" not in rendered
    if document_id:
        assert 'href="https://goodwiinz.tech/documents/doc-1"' in rendered
    else:
        assert "<a " not in rendered


def test_autoescaped_source_links_preserve_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML escaping must not drop or double-encode a source URL's ampersand."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")
    markdown = _render(
        CitationExport(
            id="citation-1",
            external_reference_id="https://example.org/paper?q=attention&view=full",
            document_title="Attention & memory",
        )
    )

    rendered = MarkdownIt("commonmark").render(markdown)
    assert (
        '<a href="https://example.org/paper?q=attention&amp;view=full">'
        "Attention &amp; memory</a>" in rendered
    )


@pytest.mark.parametrize(
    ("external_reference_id", "expected_destination"),
    [
        ("arXiv:2609.00001v2", "https://arxiv.org/abs/2609.00001v2"),
        ("doi:10.1000/example.paper", "https://doi.org/10.1000/example.paper"),
    ],
)
def test_resolvable_scholarly_identifier_gets_canonical_link(
    monkeypatch: pytest.MonkeyPatch,
    external_reference_id: str,
    expected_destination: str,
) -> None:
    """Removing a supported identifier resolver makes downloaded sources inert."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(id="citation-1", external_reference_id=external_reference_id)
    )

    assert f"]({expected_destination})**" in markdown


@pytest.mark.parametrize(
    ("external_reference_id", "expected_label"),
    [
        ("javascript:alert(1)", "javascript:alert\\(1\\)"),
        ("semantic-scholar-paper-[123]", "semantic-scholar-paper-\\[123\\]"),
    ],
)
def test_unsafe_or_unresolved_external_identifier_stays_readable_without_link(
    monkeypatch: pytest.MonkeyPatch,
    external_reference_id: str,
    expected_label: str,
) -> None:
    """Linking arbitrary identifiers, unsafe schemes, or credentials fails."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(id="citation-1", external_reference_id=external_reference_id)
    )

    assert expected_label in markdown
    assert "](" not in markdown.split("### Sources", 1)[1].split("---", 1)[0]


def test_external_url_credentials_are_neither_linked_nor_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing credential redaction leaks a secret into the downloaded file."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(
            id="citation-1",
            external_reference_id="https://reader:secret@example.org/paper",
        )
    )

    source_section = markdown.split("### Sources", 1)[1].split("---", 1)[0]
    assert "**Source 1**" in source_section
    assert "reader" not in source_section
    assert "secret" not in source_section
    assert "](" not in source_section


def test_expiring_signed_url_is_neither_linked_nor_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing signed-query redaction leaks an expiring storage credential."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(
            id="citation-1",
            external_reference_id=(
                "https://storage.example.org/file.pdf?expires=1750000000&sig=topsecret"
            ),
        )
    )

    source_section = markdown.split("### Sources", 1)[1].split("---", 1)[0]
    assert "**Source 1**" in source_section
    assert "1750000000" not in source_section
    assert "topsecret" not in source_section
    assert "](" not in source_section


@pytest.mark.parametrize(
    "credential_key", ["access_token", "id_token", "refresh_token"]
)
def test_oauth_fragment_credentials_are_neither_linked_nor_exported(
    monkeypatch: pytest.MonkeyPatch,
    credential_key: str,
) -> None:
    """Ignoring fragment parameters leaks OAuth credentials into the export."""
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://goodwiinz.tech")

    markdown = _render(
        CitationExport(
            id="citation-1",
            external_reference_id=(
                f"https://example.org/callback#{credential_key}=example-secret"
                "&token_type=bearer"
            ),
        )
    )

    source_section = markdown.split("### Sources", 1)[1].split("---", 1)[0]
    assert "**Source 1**" in source_section
    assert "example-secret" not in source_section
    assert credential_key not in source_section
    assert "](" not in source_section
