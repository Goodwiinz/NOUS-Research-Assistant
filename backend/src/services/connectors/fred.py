"""FRED (Federal Reserve Economic Data) connector."""

from __future__ import annotations

import os
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

_BASE_URL = "https://api.stlouisfed.org/fred"
_API_KEY_ENV = "FRED_API_KEY"


def _get_api_key() -> str:
    return os.environ.get(_API_KEY_ENV, "")


def _parse_series(series: Dict[str, Any]) -> ConnectorResult:
    """Parse a FRED series record into a ConnectorResult."""
    series_id = series.get("id", "")
    title = series.get("title", series_id)
    frequency = series.get("frequency", "")
    units = series.get("units", "")
    seasonal_adj = series.get("seasonal_adjustment", "")
    notes = series.get("notes", "")
    start = series.get("observation_start", "")
    end = series.get("observation_end", "")

    content_parts = [
        f"Title: {title}",
        f"Frequency: {frequency}",
        f"Units: {units}",
        f"Seasonal Adjustment: {seasonal_adj}",
        f"Observation Period: {start} to {end}",
    ]
    if notes:
        content_parts.append(f"Notes: {notes}")

    return ConnectorResult(
        id=series_id,
        title=title,
        source="fred",
        url=f"https://fred.stlouisfed.org/series/{series_id}",
        content="\n".join(content_parts),
        published_date=end or None,
        metadata={
            "series_id": series_id,
            "frequency": frequency,
            "units": units,
            "seasonal_adjustment": seasonal_adj,
            "observation_start": start,
            "observation_end": end,
        },
        document_type="economic_series",
    )


class FREDConnector(ExternalDBConnector):
    """Federal Reserve Economic Data (FRED) search and retrieval."""

    supported_filter_keys = frozenset({"order_by", "frequency"})

    def validate_search_filters(
        self, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        validated = super().validate_search_filters(filters)
        if any(not isinstance(value, str) for value in validated.values()):
            raise ValueError("FRED filters must be strings")
        return validated

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="fred",
            display_name="FRED",
            description="Federal Reserve Economic Data - 800K+ economic time series",
            domains=[ConnectorDomain.ECONOMIC, ConnectorDomain.FINANCE],
            capabilities=[ConnectorCapability.SEARCH, ConnectorCapability.FETCH],
            base_url=_BASE_URL,
            requires_api_key=True,
            api_key_env_var=_API_KEY_ENV,
            rate_limit_per_second=5.0,
            max_results_per_query=100,
        )

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        """Search FRED for economic data series matching *query*."""
        api_key = _get_api_key()
        if not api_key:
            logger.warning("fred_missing_api_key")
            return []

        params: Dict[str, Any] = {
            "search_text": query,
            "api_key": api_key,
            "file_type": "json",
            "limit": min(max_results, self.info.max_results_per_query),
        }

        if filters:
            if order := filters.get("order_by"):
                params["order_by"] = order
            if freq := filters.get("frequency"):
                params["filter_variable"] = "frequency"
                params["filter_value"] = freq

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{_BASE_URL}/series/search",
                    params=params,
                )
                resp.raise_for_status()

            data = resp.json()
            series_list = data.get("seriess", [])
            return [_parse_series(s) for s in series_list]

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "fred_search_http_error",
                status=exc.response.status_code,
                query=query,
            )
            return []
        except Exception as exc:
            logger.error("fred_search_error", error=str(exc), query=query)
            return []

    async def fetch_by_id(self, record_id: str) -> Optional[ConnectorResult]:
        """Fetch a FRED series by its series ID, including latest observations."""
        api_key = _get_api_key()
        if not api_key:
            logger.warning("fred_missing_api_key")
            return None

        common_params = {
            "api_key": api_key,
            "file_type": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Fetch series metadata
                meta_resp = await client.get(
                    f"{_BASE_URL}/series",
                    params={"series_id": record_id, **common_params},
                )
                meta_resp.raise_for_status()

                seriess = meta_resp.json().get("seriess", [])
                if not seriess:
                    return None

                result = _parse_series(seriess[0])

                # Fetch recent observations
                obs_resp = await client.get(
                    f"{_BASE_URL}/series/observations",
                    params={
                        "series_id": record_id,
                        "sort_order": "desc",
                        "limit": 10,
                        **common_params,
                    },
                )
                obs_resp.raise_for_status()

            observations = obs_resp.json().get("observations", [])
            if observations:
                obs_lines = [
                    f"  {o.get('date', '')}: {o.get('value', '')}" for o in observations
                ]
                result.content += "\n\nRecent Observations:\n" + "\n".join(obs_lines)
                result.metadata["recent_observations"] = [
                    {"date": o.get("date", ""), "value": o.get("value", "")}
                    for o in observations
                ]

            return result

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "fred_fetch_http_error",
                status=exc.response.status_code,
                series_id=record_id,
            )
            return None
        except Exception as exc:
            logger.error("fred_fetch_error", error=str(exc), series_id=record_id)
            return None
