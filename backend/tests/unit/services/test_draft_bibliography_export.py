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
        authors=[
            {"name": "Ada Lovelace"},
            {"first": "Grace", "last": "Hopper"},
            {"given": "Katherine", "family": "Johnson"},
        ],
    )
    _, parsed = _parse_export(DraftCitation(citation_index=5, citation=citation))

    assert [str(person) for person in parsed.entries["doc5"].persons["author"]] == [
        "Lovelace, Ada",
        "Hopper, Grace",
        "Johnson, Katherine",
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


def _arxiv_document() -> Document:
    # Shape of an arXiv-ingested document on dev (GOO-291 live evidence):
    # no ``year`` key, only an ISO ``publication_date``.
    return Document(
        title="Attention Is All You Need",
        arxiv_id="1706.03762v7",
        document_metadata={
            "authors": ["Ashish Vaswani", "Noam Shazeer"],
            "publication_date": "2017-06-12T17:57:34+00:00",
            "arxiv_id": "1706.03762v7",
        },
    )


def test_arxiv_document_exports_year_from_publication_date() -> None:
    """GOO-291: arXiv documents store ``publication_date``, not ``year``.

    Mutation check (2026-09-30): replacing ``publication_year(metadata)`` with
    the old ``metadata.get("year")`` in ``_canonical_citation_records`` makes
    this test fail (BibTeX entry has no ``year`` field; APA line has no
    ``(2017).``). Restoring the fallback makes it pass.
    """
    citation = DraftCitation(citation_index=1, document=_arxiv_document())

    _, parsed = _parse_export(citation)
    assert parsed.entries["doc1"].fields["year"] == "2017"

    apa = DraftGenerationService(None)._generate_markdown_references([citation], "apa")
    assert "(2017)." in apa


def test_canonical_citation_row_without_year_falls_back_to_document() -> None:
    citation = DraftCitation(
        citation_index=2,
        citation=Citation(document_title="Attention Is All You Need"),
        document=_arxiv_document(),
    )

    _, parsed = _parse_export(citation)
    assert parsed.entries["doc2"].fields["year"] == "2017"


def test_agent_bibliography_export_derives_year_via_shared_helper() -> None:
    """The agent export tool uses the same year helper as draft exports.

    Mutation check (2026-09-30): setting ``year=None`` in
    ``_citations_from_documents`` makes this test fail. Restoring
    ``publication_year(meta)`` makes it pass.
    """
    from src.services.agent.tools_impl import _citations_from_documents

    crossref = Document(
        title="Crossref paper", document_metadata={"published": [[2019, 5, 1]]}
    )
    proxies = _citations_from_documents([_arxiv_document(), crossref])

    assert [proxy.year for proxy in proxies] == [2017, 2019]
