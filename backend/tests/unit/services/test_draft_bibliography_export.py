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
