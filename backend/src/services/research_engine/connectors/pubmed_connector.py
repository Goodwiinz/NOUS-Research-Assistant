"""PubMed source connector via NCBI E-Utilities."""

import re
from typing import Any, Dict, List, Optional

import httpx
from defusedxml import ElementTree as ET

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.connectors.provider_http import get

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

_PMID_RE = re.compile(r"^\d{1,8}$")
MAX_RESULTS_LIMIT = 200


class PubMedConnector(SourceConnector):
    """Connector for the PubMed/NCBI E-Utilities API."""

    endpoint = ESEARCH_URL

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> List[SourceDocument]:
        """Search PubMed: esearch for PMIDs, then efetch for full records."""
        trace = search_trace
        max_results = min(max_results, MAX_RESULTS_LIMIT)

        base_params: Dict[str, Any] = {}
        if self.api_key:
            base_params["api_key"] = self.api_key

        # Step 1: Search for PMIDs
        search_params = {
            **base_params,
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "xml",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            if trace is not None:
                await trace.begin_request(ESEARCH_URL, search_params)
            search_resp = await get(
                client,
                ESEARCH_URL,
                provider="pubmed",
                params=search_params,
                search_trace=trace,
            )
            search_resp.raise_for_status()

            try:
                root = ET.fromstring(search_resp.text)
            except ET.ParseError as exc:
                raise ValueError(
                    f"PubMedConnector: failed to parse esearch XML: {exc}"
                ) from exc
            pmids = [
                el.text
                for el in root.findall(".//IdList/Id")
                if el.text and _PMID_RE.match(el.text)
            ]
            total_count = int(root.findtext(".//Count", default="0") or 0)

            if trace is not None:
                await trace.record_page(
                    endpoint=ESEARCH_URL,
                    params=search_params,
                    response=search_resp,
                    total_available=total_count,
                    next_cursor=None,
                    has_more=total_count > len(pmids),
                    provider_count=len(pmids),
                )

            if not pmids:
                return []

            # Step 2: Fetch full records
            fetch_params = {
                **base_params,
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "xml",
                "rettype": "abstract",
            }
            if trace is not None:
                await trace.begin_request(EFETCH_URL, fetch_params)
            fetch_resp = await get(
                client,
                EFETCH_URL,
                provider="pubmed",
                params=fetch_params,
                search_trace=trace,
            )
            fetch_resp.raise_for_status()

            documents = self._parse_articles(fetch_resp.text)
            if trace is not None:
                await trace.record_page(
                    endpoint=EFETCH_URL,
                    params=fetch_params,
                    response=fetch_resp,
                    documents=documents,
                    total_available=total_count,
                    next_cursor=None,
                    has_more=total_count > len(pmids),
                    provider_count=len(pmids),
                )
            return documents

    def _parse_articles(self, xml_text: str) -> List[SourceDocument]:
        """Parse PubMed XML into SourceDocument list."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(
                f"PubMedConnector: failed to parse efetch XML: {exc}"
            ) from exc
        documents: List[SourceDocument] = []

        for article_el in root.findall(".//PubmedArticle"):
            citation = article_el.find("MedlineCitation")
            if citation is None:
                continue

            pmid = citation.findtext("PMID", default="")
            article = citation.find("Article")
            if article is None:
                continue

            title_el = article.find("ArticleTitle")
            title = "".join(title_el.itertext()) if title_el is not None else ""
            abstract_parts = [
                "".join(el.itertext()) for el in article.findall(".//AbstractText")
            ]
            abstract = " ".join(abstract_parts)

            # Authors
            authors: List[str] = []
            for author_el in article.findall(".//Author"):
                fore = author_el.findtext("ForeName", default="")
                last = author_el.findtext("LastName", default="")
                name = f"{fore} {last}".strip()
                if name:
                    authors.append(name)

            # MeSH terms
            mesh_terms: List[str] = []
            for mesh_el in citation.findall(".//MeshHeading/DescriptorName"):
                if mesh_el.text:
                    mesh_terms.append(mesh_el.text)

            documents.append(
                SourceDocument(
                    connector_type="pubmed",
                    external_id=pmid,
                    title=title,
                    authors=authors,
                    abstract=abstract or None,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                    metadata={
                        "pmid": pmid,
                        "mesh_terms": mesh_terms,
                        "doi": article_el.findtext(".//ArticleId[@IdType='doi']"),
                        "pmcid": article_el.findtext(".//ArticleId[@IdType='pmc']"),
                        "publication_type": [
                            el.text
                            for el in article.findall(".//PublicationType")
                            if el.text
                        ],
                    },
                )
            )

        return documents
