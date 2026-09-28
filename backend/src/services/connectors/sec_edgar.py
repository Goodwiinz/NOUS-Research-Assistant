"""SEC EDGAR full-text search connector."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

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

_BASE_URL = "https://efts.sec.gov/LATEST"
_SEARCH_URL = f"{_BASE_URL}/search-index"
_FILING_BASE = "https://www.sec.gov/Archives/edgar/data"
_USER_AGENT = "NOUS Research Agent (admin@multimodal-rag.com)"


def _parse_filing(hit: Dict[str, Any]) -> ConnectorResult:
    """Parse a single EDGAR full-text search hit into a ConnectorResult."""
    source_data = hit.get("_source", hit)

    file_num = source_data.get("file_num", "")
    cik = str(
        source_data.get(
            "entity_id",
            (
                source_data.get("ciks", [""])[0]
                if isinstance(source_data.get("ciks"), list)
                else ""
            ),
        )
    )
    form_type = source_data.get("form_type", source_data.get("file_type", ""))
    company = source_data.get(
        "entity_name",
        (
            source_data.get("display_names", [""])[0]
            if isinstance(source_data.get("display_names"), list)
            else ""
        ),
    )
    date_filed = source_data.get("file_date", source_data.get("period_of_report", ""))
    accession = source_data.get("accession_no", source_data.get("_id", ""))
    description = source_data.get("file_description", "")

    # Construct a browsable URL
    accession_clean = accession.replace("-", "")
    url = (
        f"{_FILING_BASE}/{cik}/{accession_clean}/{accession}-index.htm"
        if cik and accession
        else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    )

    content_parts = [
        f"Company: {company}",
        f"Form Type: {form_type}",
        f"Filed: {date_filed}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if file_num:
        content_parts.append(f"File Number: {file_num}")

    return ConnectorResult(
        id=accession or cik,
        title=f"{company} - {form_type}" if company else form_type,
        source="sec_edgar",
        url=url,
        content="\n".join(content_parts),
        published_date=date_filed,
        metadata={
            "cik": cik,
            "form_type": form_type,
            "company_name": company,
            "accession_number": accession,
            "file_number": file_num,
        },
        document_type="filing",
    )


class SECEdgarConnector(ExternalDBConnector):
    """SEC EDGAR full-text search for public company filings."""

    supported_filter_keys = frozenset({"start_date", "end_date", "form_type"})

    def validate_search_filters(
        self, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        validated = super().validate_search_filters(filters)
        if any(not isinstance(value, str) for value in validated.values()):
            raise ValueError("SEC EDGAR filters must be strings")
        parsed_dates = {}
        for key in ("start_date", "end_date"):
            value = validated.get(key)
            if value is not None:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError(f"filter '{key}' must be an ISO calendar date")
                try:
                    parsed_dates[key] = date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(
                        f"filter '{key}' must be an ISO calendar date"
                    ) from exc
        start = parsed_dates.get("start_date")
        end = parsed_dates.get("end_date")
        if start and end and start > end:
            raise ValueError("start_date must not be after end_date")
        return validated

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="sec_edgar",
            display_name="SEC EDGAR",
            description="U.S. Securities and Exchange Commission filing database",
            domains=[ConnectorDomain.FINANCE],
            capabilities=[ConnectorCapability.SEARCH, ConnectorCapability.FETCH],
            base_url=_BASE_URL,
            rate_limit_per_second=10.0,
            max_results_per_query=100,
        )

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        """Search EDGAR full-text index for filings matching *query*."""
        params: Dict[str, Any] = {
            "q": query,
            "from": 0,
            "size": min(max_results, self.info.max_results_per_query),
        }

        # Optional date filters
        if filters:
            if start := filters.get("start_date"):
                params["startdt"] = start
            if end := filters.get("end_date"):
                params["enddt"] = end
            if form_type := filters.get("form_type"):
                params["forms"] = form_type

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _SEARCH_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                )
                resp.raise_for_status()

            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            return [_parse_filing(h) for h in hits]

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "sec_edgar_search_http_error",
                status=exc.response.status_code,
                query=query,
            )
            return []
        except Exception as exc:
            logger.error("sec_edgar_search_error", error=str(exc), query=query)
            return []

    async def fetch_by_id(self, record_id: str) -> Optional[ConnectorResult]:
        """Fetch filing details by accession number.

        Uses the full-text search API scoped to the exact accession number.
        """
        params = {"q": f'"{record_id}"', "from": 0, "size": 1}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _SEARCH_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                )
                resp.raise_for_status()

            hits = resp.json().get("hits", {}).get("hits", [])
            if not hits:
                return None
            return _parse_filing(hits[0])

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "sec_edgar_fetch_http_error",
                status=exc.response.status_code,
                accession=record_id,
            )
            return None
        except Exception as exc:
            logger.error("sec_edgar_fetch_error", error=str(exc), accession=record_id)
            return None
