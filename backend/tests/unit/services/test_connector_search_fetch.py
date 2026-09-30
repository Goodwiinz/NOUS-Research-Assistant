"""Unit tests for external database connector search/fetch methods.

Covers the parsing logic and HTTP interaction for each connector.
All network I/O is mocked — no external calls are made.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_http_client(responses: list) -> MagicMock:
    """Return a MagicMock httpx.AsyncClient whose .get() returns *responses* in order."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=responses)
    return client


def _json_resp(payload: Any) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _text_resp(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _http_error_resp(status_code: int) -> MagicMock:
    import httpx

    req = httpx.Request("GET", "https://example.com")
    raw = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=raw)


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

_PUBMED_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>CRISPR-Cas9 genome editing review</ArticleTitle>
        <Abstract>
          <AbstractText>A comprehensive review of CRISPR.</AbstractText>
        </Abstract>
        <Journal>
          <Title>Nature Reviews Genetics</Title>
        </Journal>
        <AuthorList>
          <Author>
            <LastName>Zhang</LastName>
            <Initials>F</Initials>
          </Author>
          <Author>
            <LastName>Doudna</LastName>
            <Initials>JA</Initials>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1038/nrg.2019.1</ArticleId>
        <ArticleId IdType="pubmed">12345678</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pubmed_search_parses_xml_results():
    from src.services.connectors.pubmed import PubMedConnector

    esearch_payload = {"esearchresult": {"idlist": ["12345678"]}}
    fake_client = _make_http_client(
        [_json_resp(esearch_payload), _text_resp(_PUBMED_XML)]
    )

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = PubMedConnector()
        results = await connector.search("CRISPR", max_results=5)

    assert len(results) == 1
    r = results[0]
    assert r.source == "pubmed"
    assert r.id == "12345678"
    assert "CRISPR" in r.title
    assert any("Zhang" in a for a in r.authors)
    assert any("Doudna" in a for a in r.authors)
    assert r.url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert r.metadata["doi"] == "10.1038/nrg.2019.1"
    assert r.metadata["journal"] == "Nature Reviews Genetics"
    assert "Abstract" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pubmed_search_empty_pmid_list():
    from src.services.connectors.pubmed import PubMedConnector

    esearch_payload = {"esearchresult": {"idlist": []}}
    fake_client = _make_http_client([_json_resp(esearch_payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = PubMedConnector()
        results = await connector.search("nothing")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pubmed_search_http_error_returns_empty():
    from src.services.connectors.pubmed import PubMedConnector

    fake_client = _make_http_client([_http_error_resp(429)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = PubMedConnector()
        results = await connector.search("query")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pubmed_fetch_by_id_parses_article():
    from src.services.connectors.pubmed import PubMedConnector

    fake_client = _make_http_client([_text_resp(_PUBMED_XML)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = PubMedConnector()
        result = await connector.fetch_by_id("12345678")

    assert result is not None
    assert result.id == "12345678"
    assert "CRISPR" in result.title


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pubmed_fetch_by_id_missing_article_returns_none():
    from src.services.connectors.pubmed import PubMedConnector

    empty_xml = "<PubmedArticleSet></PubmedArticleSet>"
    fake_client = _make_http_client([_text_resp(empty_xml)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = PubMedConnector()
        result = await connector.fetch_by_id("99999999")

    assert result is None


@pytest.mark.unit
def test_pubmed_api_key_params_with_env_var():
    from src.services.connectors.pubmed import PubMedConnector

    with patch.dict(os.environ, {"NCBI_API_KEY": "test-ncbi-key"}):
        connector = PubMedConnector()
        params = connector._api_key_params()

    assert params == {"api_key": "test-ncbi-key"}


@pytest.mark.unit
def test_pubmed_api_key_params_without_env_var():
    from src.services.connectors.pubmed import PubMedConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NCBI_API_KEY", None)
        connector = PubMedConnector()
        params = connector._api_key_params()

    assert params == {}


# ---------------------------------------------------------------------------
# ClinicalTrials
# ---------------------------------------------------------------------------

_CT_STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT01234567",
            "briefTitle": "Phase 3 Trial of Drug X",
        },
        "statusModule": {
            "overallStatus": "RECRUITING",
            "startDateStruct": {"date": "2022-01"},
        },
        "conditionsModule": {"conditions": ["Type 2 Diabetes"]},
        "armsInterventionsModule": {
            "interventions": [{"name": "Drug X 100mg"}, {"name": "Placebo"}]
        },
        "designModule": {
            "phases": ["PHASE3"],
            "studyType": "INTERVENTIONAL",
            "enrollmentInfo": {"count": 500},
        },
        "descriptionModule": {"briefSummary": "A trial of Drug X in T2D patients."},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Pharma Corp"}},
    }
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clinical_trials_search_parses_studies():
    from src.services.connectors.clinical_trials import ClinicalTrialsConnector

    payload = {"studies": [_CT_STUDY]}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ClinicalTrialsConnector()
        results = await connector.search("diabetes", max_results=10)

    assert len(results) == 1
    r = results[0]
    assert r.id == "NCT01234567"
    assert "Phase 3 Trial" in r.title
    assert r.source == "clinical_trials"
    assert r.document_type == "clinical_trial"
    assert "Pharma Corp" in r.authors
    assert r.metadata["status"] == "RECRUITING"
    assert r.metadata["nct_id"] == "NCT01234567"
    assert "PHASE3" in r.metadata["phase"]
    assert "RECRUITING" in r.content
    assert "Drug X 100mg" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clinical_trials_search_applies_filters():
    from src.services.connectors.clinical_trials import ClinicalTrialsConnector

    payload = {"studies": []}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ClinicalTrialsConnector()
        await connector.search(
            "cancer",
            filters={
                "status": "RECRUITING",
                "phase": "PHASE3",
                "condition": "Lung Cancer",
            },
        )

    call_kwargs = fake_client.get.call_args
    params = (
        call_kwargs.kwargs.get("params") or call_kwargs.args[1]
        if len(call_kwargs.args) > 1
        else {}
    )
    if not params and call_kwargs.kwargs:
        params = call_kwargs.kwargs.get("params", {})

    assert params.get("filter.overallStatus") == "RECRUITING"
    assert params.get("filter.phase") == "PHASE3"
    assert params.get("query.cond") == "Lung Cancer"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clinical_trials_search_http_error_returns_empty():
    from src.services.connectors.clinical_trials import ClinicalTrialsConnector

    fake_client = _make_http_client([_http_error_resp(403)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ClinicalTrialsConnector()
        results = await connector.search("query")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clinical_trials_fetch_by_id_parses_study():
    from src.services.connectors.clinical_trials import ClinicalTrialsConnector

    fake_client = _make_http_client([_json_resp(_CT_STUDY)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ClinicalTrialsConnector()
        result = await connector.fetch_by_id("NCT01234567")

    assert result is not None
    assert result.id == "NCT01234567"
    assert result.url == "https://clinicaltrials.gov/study/NCT01234567"


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------

_EDGAR_HIT = {
    "_source": {
        "accession_no": "0000123456-23-000001",
        "entity_id": "789",
        "entity_name": "Acme Corp",
        "form_type": "10-K",
        "file_date": "2023-01-15",
        "file_num": "001-12345",
        "file_description": "Annual report",
    }
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_edgar_search_parses_filings():
    from src.services.connectors.sec_edgar import SECEdgarConnector

    payload = {"hits": {"hits": [_EDGAR_HIT]}}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = SECEdgarConnector()
        results = await connector.search("annual report", max_results=5)

    assert len(results) == 1
    r = results[0]
    assert r.source == "sec_edgar"
    assert r.document_type == "filing"
    assert "Acme Corp" in r.title
    assert "10-K" in r.title
    assert r.metadata["form_type"] == "10-K"
    assert r.metadata["company_name"] == "Acme Corp"
    assert r.metadata["cik"] == "789"
    assert "Company: Acme Corp" in r.content
    assert "Filed: 2023-01-15" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_edgar_search_constructs_filing_url():
    from src.services.connectors.sec_edgar import SECEdgarConnector

    payload = {"hits": {"hits": [_EDGAR_HIT]}}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = SECEdgarConnector()
        results = await connector.search("query")

    r = results[0]
    # URL must contain CIK and accession number with dashes stripped
    assert "789" in r.url
    assert "000012345623000001" in r.url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_edgar_search_applies_filters():
    from src.services.connectors.sec_edgar import SECEdgarConnector

    payload = {"hits": {"hits": []}}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = SECEdgarConnector()
        await connector.search(
            "10-K",
            filters={
                "start_date": "2023-01-01",
                "end_date": "2023-12-31",
                "form_type": "10-K",
            },
        )

    call_kwargs = fake_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params.get("startdt") == "2023-01-01"
    assert params.get("enddt") == "2023-12-31"
    assert params.get("forms") == "10-K"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_edgar_search_empty_hits():
    from src.services.connectors.sec_edgar import SECEdgarConnector

    payload = {"hits": {"hits": []}}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = SECEdgarConnector()
        results = await connector.search("nothing")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_edgar_fetch_by_id_returns_first_hit():
    from src.services.connectors.sec_edgar import SECEdgarConnector

    payload = {"hits": {"hits": [_EDGAR_HIT]}}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = SECEdgarConnector()
        result = await connector.fetch_by_id("0000123456-23-000001")

    assert result is not None
    assert "Acme Corp" in result.title


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sec_edgar_fetch_by_id_no_hits_returns_none():
    from src.services.connectors.sec_edgar import SECEdgarConnector

    payload = {"hits": {"hits": []}}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = SECEdgarConnector()
        result = await connector.fetch_by_id("0000000000-00-000000")

    assert result is None


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------

_FRED_SERIES = {
    "id": "CPIAUCSL",
    "title": "Consumer Price Index for All Urban Consumers: All Items",
    "frequency": "Monthly",
    "units": "Index 1982-1984=100",
    "seasonal_adjustment": "Seasonally Adjusted",
    "observation_start": "1947-01-01",
    "observation_end": "2023-12-01",
    "notes": "Measures changes in the price level of a weighted average market basket.",
}

_FRED_OBSERVATIONS = {
    "observations": [
        {"date": "2023-12-01", "value": "306.746"},
        {"date": "2023-11-01", "value": "305.494"},
    ]
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fred_search_without_api_key_returns_empty():
    from src.services.connectors.fred import FREDConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FRED_API_KEY", None)
        connector = FREDConnector()
        results = await connector.search("inflation")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fred_search_parses_series():
    from src.services.connectors.fred import FREDConnector

    payload = {"seriess": [_FRED_SERIES]}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch.dict(os.environ, {"FRED_API_KEY": "test-fred-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = FREDConnector()
            results = await connector.search("CPI", max_results=5)

    assert len(results) == 1
    r = results[0]
    assert r.id == "CPIAUCSL"
    assert "Consumer Price Index" in r.title
    assert r.source == "fred"
    assert r.document_type == "economic_series"
    assert r.url == "https://fred.stlouisfed.org/series/CPIAUCSL"
    assert r.metadata["frequency"] == "Monthly"
    assert "Monthly" in r.content
    assert "Index 1982-1984=100" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fred_search_with_filters():
    from src.services.connectors.fred import FREDConnector

    payload = {"seriess": []}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = FREDConnector()
            await connector.search(
                "GDP", filters={"order_by": "popularity", "frequency": "Annual"}
            )

    call_kwargs = fake_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params.get("order_by") == "popularity"
    assert params.get("filter_variable") == "frequency"
    assert params.get("filter_value") == "Annual"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fred_fetch_by_id_without_api_key_returns_none():
    from src.services.connectors.fred import FREDConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FRED_API_KEY", None)
        connector = FREDConnector()
        result = await connector.fetch_by_id("CPIAUCSL")

    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fred_fetch_by_id_appends_observations():
    from src.services.connectors.fred import FREDConnector

    meta_payload = {"seriess": [_FRED_SERIES]}
    obs_payload = _FRED_OBSERVATIONS
    fake_client = _make_http_client([_json_resp(meta_payload), _json_resp(obs_payload)])

    with patch.dict(os.environ, {"FRED_API_KEY": "test-fred-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = FREDConnector()
            result = await connector.fetch_by_id("CPIAUCSL")

    assert result is not None
    assert result.id == "CPIAUCSL"
    assert "Recent Observations" in result.content
    assert "2023-12-01: 306.746" in result.content
    assert "recent_observations" in result.metadata


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fred_fetch_by_id_empty_seriess_returns_none():
    from src.services.connectors.fred import FREDConnector

    fake_client = _make_http_client([_json_resp({"seriess": []})])

    with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = FREDConnector()
            result = await connector.fetch_by_id("UNKNOWN")

    assert result is None


# ---------------------------------------------------------------------------
# UniProt
# ---------------------------------------------------------------------------

_UNIPROT_ENTRY = {
    "primaryAccession": "P04637",
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "proteinDescription": {
        "recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}
    },
    "organism": {"scientificName": "Homo sapiens"},
    "sequence": {"length": 393, "molWeight": 43653},
    "comments": [
        {
            "commentType": "FUNCTION",
            "texts": [{"value": "Acts as a tumor suppressor in many tumor types."}],
        }
    ],
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uniprot_search_parses_entries():
    from src.services.connectors.uniprot import UniProtConnector

    payload = {"results": [_UNIPROT_ENTRY]}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = UniProtConnector()
        results = await connector.search("TP53")

    assert len(results) == 1
    r = results[0]
    assert r.id == "P04637"
    assert r.title == "Cellular tumor antigen p53"
    assert r.source == "uniprot"
    assert r.document_type == "protein"
    assert r.url == "https://www.uniprot.org/uniprotkb/P04637"
    assert r.metadata["reviewed"] is True
    assert r.metadata["length"] == 393
    assert r.metadata["organism"] == "Homo sapiens"
    assert "393 aa" in r.content
    assert "Homo sapiens" in r.content
    assert "tumor suppressor" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uniprot_search_applies_organism_filter():
    from src.services.connectors.uniprot import UniProtConnector

    payload = {"results": []}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = UniProtConnector()
        await connector.search("kinase", filters={"organism": "Homo sapiens"})

    call_kwargs = fake_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params.get("query") == 'kinase AND organism_name:"Homo sapiens"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uniprot_direct_search_rejects_boolean_filter_before_http() -> None:
    from src.services.connectors.uniprot import UniProtConnector

    fake_client = _make_http_client([_json_resp({"results": []})])

    with patch("httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(ValueError, match="query syntax"):
            await UniProtConnector().search(
                "kinase", filters={"organism": "Homo sapiens OR Mus musculus"}
            )

    fake_client.get.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uniprot_search_http_error_returns_empty():
    from src.services.connectors.uniprot import UniProtConnector

    fake_client = _make_http_client([_http_error_resp(503)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = UniProtConnector()
        results = await connector.search("query")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uniprot_fetch_by_id_parses_entry():
    from src.services.connectors.uniprot import UniProtConnector

    fake_client = _make_http_client([_json_resp(_UNIPROT_ENTRY)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = UniProtConnector()
        result = await connector.fetch_by_id("P04637")

    assert result is not None
    assert result.id == "P04637"
    assert "p53" in result.title


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uniprot_entry_without_recommended_name_uses_accession():
    from src.services.connectors.uniprot import UniProtConnector

    entry = {
        "primaryAccession": "A0A000",
        "proteinDescription": {},
        "organism": {},
        "sequence": {},
        "comments": [],
    }
    payload = {"results": [entry]}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = UniProtConnector()
        results = await connector.search("unknown")

    assert results[0].title == "A0A000"


# ---------------------------------------------------------------------------
# ChEMBL
# ---------------------------------------------------------------------------

_CHEMBL_MOLECULE = {
    "molecule_chembl_id": "CHEMBL25",
    "pref_name": "ASPIRIN",
    "molecule_type": "Small molecule",
    "max_phase": 4,
    "indication_class": "Analgesic",
    "molecule_structures": {
        "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
        "standard_inchi_key": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
    },
    "molecule_properties": {
        "full_mwt": "180.16",
        "alogp": "1.31",
        "ro5_pass": True,
    },
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chembl_search_parses_molecules():
    from src.services.connectors.chembl import ChEMBLConnector

    payload = {"molecules": [_CHEMBL_MOLECULE]}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ChEMBLConnector()
        results = await connector.search("aspirin")

    assert len(results) == 1
    r = results[0]
    assert r.id == "CHEMBL25"
    assert r.title == "ASPIRIN"
    assert r.source == "chembl"
    assert r.document_type == "compound"
    assert r.url == "https://www.ebi.ac.uk/chembl/compound_report_card/CHEMBL25/"
    assert r.metadata["chembl_id"] == "CHEMBL25"
    assert r.metadata["smiles"] == "CC(=O)Oc1ccccc1C(=O)O"
    assert r.metadata["inchi_key"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert "CC(=O)Oc1ccccc1C(=O)O" in r.content
    assert "180.16" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chembl_search_molecule_without_pref_name_uses_chembl_id():
    from src.services.connectors.chembl import ChEMBLConnector

    mol = {"molecule_chembl_id": "CHEMBL999", "pref_name": None}
    fake_client = _make_http_client([_json_resp({"molecules": [mol]})])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ChEMBLConnector()
        results = await connector.search("compound")

    assert results[0].title == "CHEMBL999"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chembl_search_http_error_returns_empty():
    from src.services.connectors.chembl import ChEMBLConnector

    fake_client = _make_http_client([_http_error_resp(500)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ChEMBLConnector()
        results = await connector.search("drug")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chembl_fetch_by_id_returns_molecule():
    from src.services.connectors.chembl import ChEMBLConnector

    fake_client = _make_http_client([_json_resp(_CHEMBL_MOLECULE)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ChEMBLConnector()
        result = await connector.fetch_by_id("CHEMBL25")

    assert result is not None
    assert result.id == "CHEMBL25"
    assert result.title == "ASPIRIN"


# ---------------------------------------------------------------------------
# ZINC
# ---------------------------------------------------------------------------

_ZINC_SUBSTANCE = {
    "zinc_id": "ZINC000000895490",
    "smiles": "CC(=O)Oc1ccccc1C(=O)O",
    "mwt": 180.159,
    "logp": 1.31,
    "rb": 3,
    "hba": 3,
    "hbd": 1,
    "purchasable": True,
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zinc_search_parses_list_response():
    from src.services.connectors.zinc import ZINCConnector

    fake_client = _make_http_client([_json_resp([_ZINC_SUBSTANCE])])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ZINCConnector()
        results = await connector.search("aspirin")

    assert len(results) == 1
    r = results[0]
    assert r.id == "ZINC000000895490"
    assert r.title == "ZINC000000895490"
    assert r.source == "zinc"
    assert r.document_type == "compound"
    assert "CC(=O)Oc1ccccc1C(=O)O" in r.content
    assert "180.159" in r.content
    assert r.metadata["purchasable"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zinc_search_parses_dict_response():
    from src.services.connectors.zinc import ZINCConnector

    payload = {"results": [_ZINC_SUBSTANCE]}
    fake_client = _make_http_client([_json_resp(payload)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ZINCConnector()
        results = await connector.search("aspirin")

    assert len(results) == 1
    assert results[0].id == "ZINC000000895490"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zinc_search_http_error_returns_empty():
    from src.services.connectors.zinc import ZINCConnector

    fake_client = _make_http_client([_http_error_resp(404)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ZINCConnector()
        results = await connector.search("query")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zinc_substance_without_zinc_id_falls_back_to_preferred_name():
    from src.services.connectors.zinc import ZINCConnector

    substance = {"preferred_name": "MyCompound", "smiles": "CC"}
    fake_client = _make_http_client([_json_resp([substance])])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ZINCConnector()
        results = await connector.search("compound")

    assert results[0].title == "MyCompound"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zinc_fetch_by_id_returns_substance():
    from src.services.connectors.zinc import ZINCConnector

    fake_client = _make_http_client([_json_resp(_ZINC_SUBSTANCE)])

    with patch("httpx.AsyncClient", return_value=fake_client):
        connector = ZINCConnector()
        result = await connector.fetch_by_id("ZINC000000895490")

    assert result is not None
    assert result.id == "ZINC000000895490"


# ---------------------------------------------------------------------------
# Alpha Vantage
# ---------------------------------------------------------------------------

_AV_SYMBOL_SEARCH = {
    "bestMatches": [
        {
            "1. symbol": "AAPL",
            "2. name": "Apple Inc",
            "3. type": "Equity",
            "4. region": "United States",
            "8. currency": "USD",
            "9. matchScore": "1.0000",
        }
    ]
}

_AV_GLOBAL_QUOTE = {
    "Global Quote": {
        "01. symbol": "AAPL",
        "05. price": "189.30",
        "06. volume": "55000000",
        "07. latest trading day": "2023-12-29",
        "09. change": "1.25",
        "10. change percent": "0.6644%",
    }
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alpha_vantage_search_without_api_key_returns_empty():
    from src.services.connectors.alpha_vantage import AlphaVantageConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALPHA_VANTAGE_API_KEY", None)
        connector = AlphaVantageConnector()
        results = await connector.search("Apple")

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alpha_vantage_search_parses_best_matches():
    from src.services.connectors.alpha_vantage import AlphaVantageConnector

    fake_client = _make_http_client([_json_resp(_AV_SYMBOL_SEARCH)])

    with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-av-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = AlphaVantageConnector()
            results = await connector.search("Apple")

    assert len(results) == 1
    r = results[0]
    assert r.id == "AAPL"
    assert "AAPL" in r.title
    assert "Apple Inc" in r.title
    assert r.source == "alpha_vantage"
    assert r.document_type == "security"
    assert r.metadata["symbol"] == "AAPL"
    assert r.metadata["currency"] == "USD"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alpha_vantage_fetch_by_id_without_api_key_returns_none():
    from src.services.connectors.alpha_vantage import AlphaVantageConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALPHA_VANTAGE_API_KEY", None)
        connector = AlphaVantageConnector()
        result = await connector.fetch_by_id("AAPL")

    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alpha_vantage_fetch_by_id_parses_global_quote():
    from src.services.connectors.alpha_vantage import AlphaVantageConnector

    fake_client = _make_http_client([_json_resp(_AV_GLOBAL_QUOTE)])

    with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-av-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = AlphaVantageConnector()
            result = await connector.fetch_by_id("AAPL")

    assert result is not None
    assert result.id == "AAPL"
    assert result.title == "AAPL Quote"
    assert result.document_type == "quote"
    assert "189.30" in result.content
    assert "55000000" in result.content
    assert result.metadata["symbol"] == "AAPL"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alpha_vantage_fetch_by_id_empty_quote_returns_none():
    from src.services.connectors.alpha_vantage import AlphaVantageConnector

    fake_client = _make_http_client([_json_resp({"Global Quote": {}})])

    with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = AlphaVantageConnector()
            result = await connector.fetch_by_id("INVALID")

    assert result is None


# ---------------------------------------------------------------------------
# COSMIC
# ---------------------------------------------------------------------------

_COSMIC_GENE = {
    "gene_symbol": "TP53",
    "gene_name": "Tumour protein P53",
    "chromosome": "17",
    "mutation_count": 45000,
    "sample_count": 20000,
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmic_search_unauthenticated_returns_placeholder():
    from src.services.connectors.cosmic import COSMICConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COSMIC_AUTH", None)
        connector = COSMICConnector()
        results = await connector.search("TP53")

    assert len(results) == 1
    r = results[0]
    assert r.id == "TP53"
    assert "COSMIC" in r.title
    assert "auth_required" in r.metadata
    assert r.metadata["auth_required"] is True
    assert "COSMIC_AUTH" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmic_search_authenticated_parses_gene_list():
    from src.services.connectors.cosmic import COSMICConnector

    fake_client = _make_http_client([_json_resp([_COSMIC_GENE])])

    with patch.dict(os.environ, {"COSMIC_AUTH": "user@example.com:password"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = COSMICConnector()
            results = await connector.search("TP53", max_results=10)

    assert len(results) == 1
    r = results[0]
    assert r.id == "TP53"
    assert r.title == "TP53"
    assert r.source == "cosmic"
    assert r.document_type == "gene"
    assert "Chromosome: 17" in r.content
    assert "Mutation Count: 45000" in r.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmic_search_authenticated_single_dict_response():
    from src.services.connectors.cosmic import COSMICConnector

    fake_client = _make_http_client([_json_resp(_COSMIC_GENE)])

    with patch.dict(os.environ, {"COSMIC_AUTH": "user@example.com:password"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = COSMICConnector()
            results = await connector.search("TP53")

    assert len(results) == 1
    assert results[0].id == "TP53"


@pytest.mark.unit
def test_cosmic_auth_headers_with_colon_credentials():
    import base64

    from src.services.connectors.cosmic import COSMICConnector

    with patch.dict(os.environ, {"COSMIC_AUTH": "user@example.com:mypassword"}):
        connector = COSMICConnector()
        headers = connector._auth_headers()

    expected = base64.b64encode(b"user@example.com:mypassword").decode()
    assert headers == {"Authorization": f"Basic {expected}"}


@pytest.mark.unit
def test_cosmic_auth_headers_with_preencoded_token():
    from src.services.connectors.cosmic import COSMICConnector

    with patch.dict(os.environ, {"COSMIC_AUTH": "dXNlcjpwYXNz"}):
        connector = COSMICConnector()
        headers = connector._auth_headers()

    assert headers == {"Authorization": "Basic dXNlcjpwYXNz"}


@pytest.mark.unit
def test_cosmic_auth_headers_missing_env_var():
    from src.services.connectors.cosmic import COSMICConnector

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COSMIC_AUTH", None)
        connector = COSMICConnector()
        headers = connector._auth_headers()

    assert headers == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmic_search_http_error_returns_empty():
    from src.services.connectors.cosmic import COSMICConnector

    fake_client = _make_http_client([_http_error_resp(401)])

    with patch.dict(os.environ, {"COSMIC_AUTH": "user:pass"}):
        with patch("httpx.AsyncClient", return_value=fake_client):
            connector = COSMICConnector()
            results = await connector.search("BRCA1")

    assert results == []
