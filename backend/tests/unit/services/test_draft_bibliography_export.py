import pytest
from pybtex.database import BibliographyData, parse_string

from src.models.citation import Citation
from src.models.document import Document
from src.models.draft_citation import DraftCitation
from src.services.research.draft_generation_service import DraftGenerationService


def _parse_export(*citations: DraftCitation) -> tuple[str, BibliographyData]:
    exported = DraftGenerationService(None)._generate_bib_entries(list(citations))
    return exported, parse_string(exported, "bibtex")


def test_draft_bibtex_uses_canonical_citation_metadata_and_doc_key() -> None:
    canonical = Citation(
        document_title='A "Quoted" {Result}',
        authors=["Ada Lovelace", "Grace Hopper"],
        year=2026,
        venue="Journal of R&D",
        doi="10.1000/example",
    )
    exported, parsed = _parse_export(
        DraftCitation(
            citation_index=3,
            citation=canonical,
            snippet='"This quote must never become the title."',
        )
    )

    entry = parsed.entries["doc3"]
    assert entry.fields["title"] == 'A "Quoted" {Result}'
    assert entry.fields["year"] == "2026"
    assert entry.fields["journal"] == r"Journal of R\&D"
    assert entry.fields["doi"] == "10.1000/example"
    assert [str(person) for person in entry.persons["author"]] == [
        "Lovelace, Ada",
        "Hopper, Grace",
    ]
    assert "This quote must never" not in exported


def test_draft_bibtex_uses_document_metadata_without_inventing_fields() -> None:
    document = Document(
        title="Canonical Document Title",
        document_metadata={"authors": ["Leslie Lamport"]},
    )
    _, parsed = _parse_export(
        DraftCitation(
            citation_index=1,
            document=document,
            snippet="A source passage, not a paper title.",
        )
    )

    entry = parsed.entries["doc1"]
    assert entry.fields == {"title": "Canonical Document Title"}
    assert [str(person) for person in entry.persons["author"]] == ["Lamport, Leslie"]


def test_bibliography_normalizes_legacy_object_shaped_authors() -> None:
    citation = Citation(
        document_title="Object-shaped authors",
        authors=[{"name": "Ada Lovelace"}, {"first": "Grace", "last": "Hopper"}],
    )
    _, parsed = _parse_export(DraftCitation(citation_index=5, citation=citation))

    assert [str(person) for person in parsed.entries["doc5"].persons["author"]] == [
        "Lovelace, Ada",
        "Hopper, Grace",
    ]


@pytest.mark.parametrize("bib_format", ["apa", "ieee", "mla"])
def test_markdown_references_keep_sparse_doc_marker_and_canonical_metadata(
    bib_format: str,
) -> None:
    citation = Citation(
        document_title="Canonical Résumé Study",
        authors=["Ada Lovelace"],
        year=2026,
        venue="Journal of R&D",
        doi="10.1000/example",
    )
    references = DraftGenerationService(None)._generate_markdown_references(
        [
            DraftCitation(
                citation_index=7,
                citation=citation,
                snippet="Evidence passage that must not become a reference record.",
            )
        ],
        bib_format,
    )

    assert references.startswith("[Doc 7]")
    assert "Canonical Résumé Study" in references
    assert "Lovelace" in references
    assert "2026" in references
    assert "Evidence passage" not in references


@pytest.mark.parametrize("bib_format", ["bibtex", "biblatex"])
def test_markdown_bibtex_uses_stable_doc_key_and_preserves_missing_metadata(
    bib_format: str,
) -> None:
    references = DraftGenerationService(None)._generate_markdown_references(
        [
            DraftCitation(
                citation_index=4,
                document=Document(title="Sparse canonical title", document_metadata={}),
                snippet="Never a title",
            )
        ],
        bib_format,
    )

    parsed = parse_string(
        references.removeprefix("```bibtex\n").removesuffix("\n```"), "bibtex"
    )
    assert parsed.entries["doc4"].fields == {"title": "Sparse canonical title"}
    assert "Never a title" not in references


def test_markdown_reference_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="Unsupported bibliography format"):
        DraftGenerationService(None)._generate_markdown_references([], "csl-json")
