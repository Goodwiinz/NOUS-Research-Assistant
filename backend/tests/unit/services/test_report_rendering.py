"""Reader-facing projections must retain canonical source provenance."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest

from src.services.research_engine.connectors.base import SourceDocument
from src.services.research_engine.connectors.pubmed_connector import PubMedConnector
from src.services.research_engine.discovery import prepare_sources, source_records
from src.services.research_engine.report_rendering import (
    build_report,
    render_csv,
    render_markdown,
)


def _real_shaped_context() -> dict[str, Any]:
    sources = [
        {
            "source_id": "source-openalex",
            "connector_type": "openalex",
            "external_id": "https://openalex.org/W123",
            "title": "OpenAlex abstract",
            "authors": ["A. Author"],
            "abstract": "OpenAlex evidence.",
            "url": "https://doi.org/10.1000/openalex",
            "evidence_level": "abstract",
            "metadata": {
                "doi": "https://doi.org/10.1000/openalex",
                "publication_date": "2025-01-02",
                "publication_type": "article",
                "identifiers": {
                    "doi": "10.1000/openalex",
                    "openalex": "W123",
                },
            },
        },
        {
            "source_id": "source-crossref",
            "connector_type": "crossref",
            "external_id": "10.1000/crossref",
            "title": "Crossref abstract",
            "authors": ["C. Researcher"],
            "abstract": "Crossref evidence.",
            "url": "https://api.crossref.org/works/10.1000/crossref",
            "evidence_level": "abstract",
            "metadata": {
                "doi": "10.1000/crossref",
                "journal": "Journal of Bounded Evidence",
                "published": [[2024, 3, 4]],
                "identifiers": {"doi": "10.1000/crossref"},
            },
        },
        {
            "source_id": "source-pubmed",
            "connector_type": "pubmed",
            "external_id": "12345678",
            "title": "PubMed abstract",
            "authors": ["P. Investigator"],
            "abstract": "PubMed evidence.",
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
            "evidence_level": "abstract",
            "metadata": {
                "doi": "10.1000/pubmed",
                "publication_date": "2023-07-01",
                "publication_type": ["Journal Article"],
                "identifiers": {
                    "doi": "10.1000/pubmed",
                    "pmid": "12345678",
                },
            },
        },
    ]
    extractions = [
        {
            "source_id": source["source_id"],
            "part_id": "p0001",
            "data": {"finding": source["title"]},
            "evidence": [
                {
                    "evidence_id": f"e{index:04d}",
                    "part_id": "p0001",
                    "pointer": "abstract:0-8",
                    "quote": str(source["abstract"]),
                }
            ],
        }
        for index, source in enumerate(sources, start=1)
    ]
    return {
        "contract_version": 1,
        "source_records": sources,
        "extractions": extractions,
        "verification": {"passed": True, "claims": []},
    }


def test_real_connector_source_metadata_drives_json_markdown_and_csv() -> None:
    """Extraction self-labels cannot weaken the canonical source evidence level."""
    context = _real_shaped_context()
    report = build_report(context)

    by_id = {source["source_id"]: source for source in report["sources"]}
    assert by_id["source-openalex"]["doi"] == "10.1000/openalex"
    assert by_id["source-openalex"]["publication_year"] == 2025
    assert by_id["source-crossref"]["doi"] == "10.1000/crossref"
    assert by_id["source-crossref"]["publication_year"] == 2024
    assert by_id["source-crossref"]["journal"] == "Journal of Bounded Evidence"
    assert by_id["source-pubmed"]["doi"] == "10.1000/pubmed"
    assert by_id["source-pubmed"]["publication_year"] == 2023
    assert {item["evidence_level"] for item in report["evidence"]} == {"abstract"}

    markdown = render_markdown(report)
    for year, doi in (
        ("2025", "10.1000/openalex"),
        ("2024", "10.1000/crossref"),
        ("2023", "10.1000/pubmed"),
    ):
        assert year in markdown
        assert doi.replace(".", r"\.") in markdown

    sources = {source["source_id"]: source for source in context["source_records"]}
    csv_text = render_csv(
        [
            {"source": sources[item["source_id"]], "extraction": item}
            for item in context["extractions"]
        ],
        final_status="verified",
    )
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert [row["evidence_level"] for row in rows] == ["abstract"] * 3
    assert "2025" in rows[0]["bibliography"]
    assert "10.1000/openalex" in rows[0]["bibliography"]
    assert "2024" in rows[1]["bibliography"]
    assert "10.1000/crossref" in rows[1]["bibliography"]
    assert "2023" in rows[2]["bibliography"]
    assert "10.1000/pubmed" in rows[2]["bibliography"]


def test_pubmed_connector_preserves_bibliographic_date_and_journal() -> None:
    documents = PubMedConnector()._parse_articles("""
        <PubmedArticleSet><PubmedArticle><MedlineCitation>
          <PMID>12345678</PMID><Article>
            <ArticleTitle>PubMed source</ArticleTitle>
            <Abstract><AbstractText>Abstract evidence.</AbstractText></Abstract>
            <Journal><JournalIssue><PubDate><Year>2023</Year><Month>7</Month><Day>1</Day></PubDate></JournalIssue><Title>Journal Name</Title></Journal>
          </Article>
        </MedlineCitation><PubmedData><ArticleIdList>
          <ArticleId IdType="doi">10.1000/pubmed</ArticleId>
        </ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>
        """)

    assert documents[0].metadata["publication_date"] == "2023-07-01"
    assert documents[0].metadata["journal"] == "Journal Name"


@pytest.mark.parametrize("reverse", [False, True])
def test_deduplicated_provider_provenance_enriches_bibliography_without_leaking(
    reverse: bool,
) -> None:
    """A secondary provider may fill empty canonical bibliography fields."""
    openalex = SourceDocument(
        connector_type="openalex",
        external_id="https://openalex.org/W-PROVENANCE",
        title="Shared evidence source",
        authors=["O. Researcher"],
        abstract="The intervention improved the measured outcome.",
        url="https://doi.org/10.5555/provenance",
        metadata={"doi": "https://doi.org/10.5555/provenance"},
    )
    crossref = SourceDocument(
        connector_type="crossref",
        external_id="10.5555/provenance",
        title="Shared evidence source",
        metadata={
            "doi": "10.5555/provenance",
            "journal": "Journal of Provider Provenance",
            "published": [[2025, 4, 3]],
        },
    )
    documents = [crossref, openalex] if reverse else [openalex, crossref]
    records = source_records(prepare_sources(documents))
    assert len(records) == 1
    source_id = records[0]["source_id"]
    extraction = {
        "source_id": source_id,
        "part_id": "p0001",
        "data": {"finding": "Improved outcome"},
        "evidence": [
            {
                "evidence_id": "e0001",
                "part_id": "p0001",
                "pointer": "abstract:0-52",
                "quote": "The intervention improved the measured outcome.",
            }
        ],
    }
    context = {
        "contract_version": 1,
        "source_records": records,
        "extractions": [extraction],
        "verification": {"passed": True, "claims": []},
    }

    report = build_report(context)
    source = report["sources"][0]
    assert source["evidence_level"] == "abstract"
    assert source["doi"] == "10.5555/provenance"
    assert source["publication_year"] == 2025
    assert source["journal"] == "Journal of Provider Provenance"

    markdown = render_markdown(report)
    csv_text = render_csv(
        [{"source": records[0], "extraction": extraction}],
        final_status="verified",
    )
    csv_row = next(csv.DictReader(io.StringIO(csv_text)))
    assert "Journal of Provider Provenance" in markdown
    assert "2025" in markdown
    assert "10\\.5555/provenance" in markdown
    assert "Journal of Provider Provenance" in csv_row["bibliography"]
    assert "2025" in csv_row["bibliography"]
    assert "10.5555/provenance" in csv_row["bibliography"]
    assert csv_row["evidence_level"] == "abstract"

    reader_output = json.dumps(report, sort_keys=True) + markdown + csv_text
    assert '"provenance"' not in reader_output
    assert "retrieved_at" not in reader_output


def test_canonical_bibliography_values_win_over_conflicting_provenance() -> None:
    canonical = SourceDocument(
        connector_type="openalex",
        external_id="https://openalex.org/W-CONFLICT",
        title="Canonical source",
        abstract="Canonical abstract.",
        metadata={
            "doi": "10.5555/conflict",
            "journal": "Canonical Journal",
            "publication_date": "2024-02-01",
        },
    )
    secondary = SourceDocument(
        connector_type="crossref",
        external_id="10.5555/conflict",
        title="Secondary source",
        metadata={
            "doi": "10.5555/conflict",
            "journal": "Conflicting Journal",
            "published": [[2025, 1, 1]],
        },
    )
    records = source_records(prepare_sources([canonical, secondary]))

    source = build_report(
        {
            "contract_version": 1,
            "source_records": records,
            "verification": {"passed": True, "claims": []},
        }
    )["sources"][0]

    assert source["publication_year"] == 2024
    assert source["journal"] == "Canonical Journal"


@pytest.mark.parametrize(
    ("reverse", "expected_year", "expected_date", "conflicting_year"),
    [
        (False, 2025, [[2025, 6, 1]], "2024"),
        (True, 2024, "2024-01-02", "2025"),
    ],
)
def test_date_aliases_preserve_canonical_provider_across_reader_formats(
    reverse: bool,
    expected_year: int,
    expected_date: object,
    conflicting_year: str,
) -> None:
    """A secondary date alias cannot replace the canonical provider's date."""
    crossref = SourceDocument(
        connector_type="crossref",
        external_id="10.5555/date-alias",
        title="Canonical date source",
        metadata={
            "doi": "10.5555/date-alias",
            "published": [[2025, 6, 1]],
        },
    )
    openalex = SourceDocument(
        connector_type="openalex",
        external_id="https://openalex.org/W-DATE-ALIAS",
        title="Canonical date source",
        abstract="Date alias evidence.",
        metadata={
            "doi": "10.5555/date-alias",
            "publication_date": "2024-01-02",
        },
    )
    documents = [openalex, crossref] if reverse else [crossref, openalex]
    records = source_records(prepare_sources(documents))
    assert len(records) == 1
    source_id = records[0]["source_id"]
    extraction = {
        "source_id": source_id,
        "part_id": "p0001",
        "data": {"finding": "Canonical date finding"},
        "evidence": [
            {
                "evidence_id": "e0001",
                "part_id": "p0001",
                "pointer": "abstract:0-20",
                "quote": "Date alias evidence.",
            }
        ],
    }
    report = build_report(
        {
            "contract_version": 1,
            "source_records": records,
            "extractions": [extraction],
            "verification": {"passed": True, "claims": []},
        }
    )
    source = report["sources"][0]
    markdown = render_markdown(report)
    csv_text = render_csv(
        [{"source": records[0], "extraction": extraction}],
        final_status="verified",
    )
    csv_row = next(csv.DictReader(io.StringIO(csv_text)))

    assert source["publication_year"] == expected_year
    assert source["publication_date"] == expected_date
    assert str(expected_year) in markdown
    assert conflicting_year not in markdown
    assert str(expected_year) in csv_row["bibliography"]
    assert conflicting_year not in csv_row["bibliography"]


def test_prepare_sources_export_prefers_direct_doi_and_filters_opaque_identifiers() -> (
    None
):
    """Legacy nested metadata cannot let retained provider internals reach readers."""
    document = SourceDocument(
        connector_type="openalex",
        external_id="https://openalex.org/W-SAFE-IDENTIFIERS",
        title="Canonical identifier source",
        abstract="Canonical abstract evidence.",
        url="https://doi.org/10.5555/canonical",
        metadata={
            "metadata": {"doi": "https://doi.org/10.5555/canonical"},
            "identifiers": {
                "doi": "10.5555/conflicting-provenance",
                "provider_trace": "opaque-provider-trace",
                "patient_id": "opaque-patient-id",
            },
        },
    )
    records = source_records(prepare_sources([document]))
    assert len(records) == 1
    source_id = records[0]["source_id"]
    extraction = {
        "source_id": source_id,
        "part_id": "p0001",
        "data": {"finding": "Canonical finding"},
        "evidence": [
            {
                "evidence_id": "e0001",
                "part_id": "p0001",
                "pointer": "abstract:0-27",
                "quote": "Canonical abstract evidence.",
            }
        ],
    }
    report = build_report(
        {
            "contract_version": 1,
            "source_records": records,
            "extractions": [extraction],
            "verification": {"passed": True, "claims": []},
        }
    )
    source = report["sources"][0]

    assert source["doi"] == "10.5555/canonical"
    assert source["identifiers"] == {
        "doi": "https://doi.org/10.5555/canonical",
        "openalex": "W-SAFE-IDENTIFIERS",
    }

    markdown = render_markdown(report)
    csv_text = render_csv(
        [{"source": records[0], "extraction": extraction}],
        final_status="verified",
    )
    csv_row = next(csv.DictReader(io.StringIO(csv_text)))
    assert "10\\.5555/canonical" in markdown
    assert "10.5555/canonical" in csv_row["bibliography"]

    reader_output = json.dumps(report, sort_keys=True) + markdown + csv_text
    assert "10.5555/conflicting-provenance" not in reader_output
    assert "provider_trace" not in reader_output
    assert "opaque-provider-trace" not in reader_output
    assert "patient_id" not in reader_output
    assert "opaque-patient-id" not in reader_output


def test_bridged_provider_cluster_uses_retained_bibliography() -> None:
    crossref = SourceDocument(
        connector_type="crossref",
        external_id="10.5555/bridge",
        title="Crossref bridge",
        metadata={
            "doi": "10.5555/bridge",
            "journal": "Bridge Journal",
            "published": [[2022, 8, 9]],
        },
    )
    pubmed = SourceDocument(
        connector_type="pubmed",
        external_id="123456",
        title="PubMed bridge",
        metadata={"pmid": "123456"},
    )
    openalex = SourceDocument(
        connector_type="openalex",
        external_id="https://openalex.org/W-BRIDGE",
        title="OpenAlex bridge",
        abstract="Bridge abstract evidence.",
        metadata={"doi": "10.5555/bridge", "pmid": "123456"},
    )
    records = source_records(prepare_sources([crossref, pubmed, openalex]))

    assert len(records) == 1
    source = build_report(
        {
            "contract_version": 1,
            "source_records": records,
            "verification": {"passed": True, "claims": []},
        }
    )["sources"][0]
    assert source["evidence_level"] == "abstract"
    assert source["doi"] == "10.5555/bridge"
    assert source["publication_year"] == 2022
    assert source["journal"] == "Bridge Journal"
