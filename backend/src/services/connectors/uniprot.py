"""UniProt protein sequence database connector via REST API."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import structlog

from .base import (
    ConnectorCapability,
    ConnectorDomain,
    ConnectorInfo,
    ConnectorResult,
    ExternalDBConnector,
)

logger = structlog.get_logger(__name__)

_BASE_URL = "https://rest.uniprot.org"
_SEARCH_URL = f"{_BASE_URL}/uniprotkb/search"
_FETCH_URL = f"{_BASE_URL}/uniprotkb"


def _build_content(entry: Dict[str, Any]) -> str:
    parts: List[str] = []
    if length := entry.get("sequence", {}).get("length"):
        parts.append(f"Length: {length} aa")
    if mass := entry.get("sequence", {}).get("molWeight"):
        parts.append(f"Mass: {mass} Da")
    if organism := entry.get("organism", {}).get("scientificName"):
        parts.append(f"Organism: {organism}")
    for comment in entry.get("comments", [])[:3]:
        if comment.get("commentType") == "FUNCTION":
            for text in comment.get("texts", [])[:1]:
                if value := text.get("value"):
                    parts.append(f"Function: {value}")
    return "\n".join(parts)


def _parse_entry(entry: Dict[str, Any]) -> ConnectorResult:
    accession = entry.get("primaryAccession", "")
    protein_desc = entry.get("proteinDescription", {})
    rec_name = (
        protein_desc.get("recommendedName", {}).get("fullName", {}).get("value", "")
    )
    title = rec_name or accession

    organism = entry.get("organism", {}).get("scientificName", "")
    sequence = entry.get("sequence", {})

    metadata = {
        "accession": accession,
        "organism": organism,
        "length": sequence.get("length"),
        "mol_weight": sequence.get("molWeight"),
        "reviewed": entry.get("entryType") == "UniProtKB reviewed (Swiss-Prot)",
    }

    return ConnectorResult(
        id=accession,
        title=title,
        source="uniprot",
        url=f"https://www.uniprot.org/uniprotkb/{accession}",
        content=_build_content(entry),
        metadata=metadata,
        document_type="protein",
    )


class UniProtConnector(ExternalDBConnector):
    """UniProt protein sequence and functional information."""

    supported_filter_keys = frozenset({"organism"})

    def validate_search_filters(
        self, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        validated = super().validate_search_filters(filters)
        organism = validated.get("organism")
        if organism is not None and (
            not isinstance(organism, str)
            or not all(char.isalnum() or char in " _.-" for char in organism)
            or re.search(r"\b(?:AND|OR|NOT)\b", organism, flags=re.IGNORECASE)
        ):
            raise ValueError("filter 'organism' contains unsupported query syntax")
        return validated

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="uniprot",
            display_name="UniProt",
            description="Protein sequence and functional information (250M+ entries)",
            domains=[ConnectorDomain.BIOMEDICAL, ConnectorDomain.GENOMICS],
            capabilities=[ConnectorCapability.SEARCH, ConnectorCapability.FETCH],
            base_url=_BASE_URL,
            rate_limit_per_second=10.0,
            max_results_per_query=500,
        )

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        filters = self.validate_search_filters(filters)
        params: Dict[str, Any] = {
            "query": query,
            "size": min(max_results, self.info.max_results_per_query),
            "format": "json",
        }
        if filters and (org := filters.get("organism")):
            params["query"] = f'{query} AND organism_name:"{org}"'

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(_SEARCH_URL, params=params)
                resp.raise_for_status()
            entries = resp.json().get("results", [])
            return [_parse_entry(e) for e in entries]
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "uniprot_search_http_error",
                status=exc.response.status_code,
                query=query,
            )
            return []
        except Exception as exc:
            logger.error("uniprot_search_error", error=str(exc), query=query)
            return []

    async def fetch_by_id(self, record_id: str) -> Optional[ConnectorResult]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{_FETCH_URL}/{quote(record_id, safe='')}.json"
                )
                resp.raise_for_status()
            return _parse_entry(resp.json())
        except Exception as exc:
            logger.error("uniprot_fetch_error", error=str(exc), id=record_id)
            return None
