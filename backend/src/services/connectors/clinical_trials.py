"""ClinicalTrials.gov connector via API v2."""

from __future__ import annotations

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

_BASE_URL = "https://clinicaltrials.gov/api/v2"
# Note: clinicaltrials.gov uses TLS-fingerprint-based bot detection that can
# return 403 to vanilla python-httpx connections from some networks. The
# connector handles 403 gracefully; for production deploys behind a known
# egress IP, request whitelisting or use the ctypes mirror.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NOUS/1.0; +https://multimodal-rag.com)",
    "Accept": "application/json",
}


def _build_content(study: Dict[str, Any]) -> str:
    parts: List[str] = []
    proto = study.get("protocolSection", {})
    if status := proto.get("statusModule", {}).get("overallStatus"):
        parts.append(f"Status: {status}")
    if conditions := proto.get("conditionsModule", {}).get("conditions"):
        parts.append(f"Conditions: {', '.join(conditions[:5])}")
    if interventions := proto.get("armsInterventionsModule", {}).get("interventions"):
        names = [i.get("name", "") for i in interventions[:5]]
        parts.append(f"Interventions: {', '.join(filter(None, names))}")
    if phase := proto.get("designModule", {}).get("phases"):
        parts.append(f"Phase: {', '.join(phase)}")
    if (
        enrollment := proto.get("designModule", {})
        .get("enrollmentInfo", {})
        .get("count")
    ):
        parts.append(f"Enrollment: {enrollment}")
    if summary := proto.get("descriptionModule", {}).get("briefSummary"):
        parts.append(f"\nSummary: {summary[:500]}")
    return "\n".join(parts)


def _parse_study(study: Dict[str, Any]) -> ConnectorResult:
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    nct_id = ident.get("nctId", "")
    title = ident.get("briefTitle") or ident.get("officialTitle") or nct_id

    status_module = proto.get("statusModule", {})
    start_date = status_module.get("startDateStruct", {}).get("date")

    sponsors = proto.get("sponsorCollaboratorsModule", {})
    lead_sponsor = sponsors.get("leadSponsor", {}).get("name", "")

    metadata = {
        "nct_id": nct_id,
        "status": status_module.get("overallStatus"),
        "phase": proto.get("designModule", {}).get("phases"),
        "lead_sponsor": lead_sponsor,
        "study_type": proto.get("designModule", {}).get("studyType"),
    }

    return ConnectorResult(
        id=nct_id,
        title=title,
        source="clinical_trials",
        url=f"https://clinicaltrials.gov/study/{nct_id}",
        content=_build_content(study),
        authors=[lead_sponsor] if lead_sponsor else [],
        published_date=start_date,
        metadata=metadata,
        document_type="clinical_trial",
    )


class ClinicalTrialsConnector(ExternalDBConnector):
    """ClinicalTrials.gov registry of clinical studies."""

    supported_filter_keys = frozenset({"status", "phase", "condition"})

    def validate_search_filters(
        self, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        validated = super().validate_search_filters(filters)
        if any(not isinstance(value, str) for value in validated.values()):
            raise ValueError("ClinicalTrials filters must be strings")
        return validated

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="clinical_trials",
            display_name="ClinicalTrials.gov",
            description="U.S. National Library of Medicine clinical trial registry",
            domains=[ConnectorDomain.CLINICAL, ConnectorDomain.BIOMEDICAL],
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
        params: Dict[str, Any] = {
            "query.term": query,
            "pageSize": min(max_results, self.info.max_results_per_query),
            "format": "json",
        }
        if filters:
            if status := filters.get("status"):
                params["filter.overallStatus"] = status
            if phase := filters.get("phase"):
                params["filter.phase"] = phase
            if condition := filters.get("condition"):
                params["query.cond"] = condition

        try:
            async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
                resp = await client.get(f"{_BASE_URL}/studies", params=params)
                resp.raise_for_status()
            studies = resp.json().get("studies", [])
            return [_parse_study(s) for s in studies]
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "clinical_trials_http_error",
                status=exc.response.status_code,
                query=query,
            )
            return []
        except Exception as exc:
            logger.error("clinical_trials_search_error", error=str(exc), query=query)
            return []

    async def fetch_by_id(self, record_id: str) -> Optional[ConnectorResult]:
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
                resp = await client.get(
                    f"{_BASE_URL}/studies/{quote(record_id, safe='')}",
                    params={"format": "json"},
                )
                resp.raise_for_status()
            return _parse_study(resp.json())
        except Exception as exc:
            logger.error("clinical_trials_fetch_error", error=str(exc), id=record_id)
            return None
