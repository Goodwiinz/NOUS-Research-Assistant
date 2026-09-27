"""One canonical provider registry for research execution and discovery."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.core.config import settings
from src.services.research_engine.connectors.arxiv_connector import ArxivConnector
from src.services.research_engine.connectors.base import SourceConnector
from src.services.research_engine.connectors.crossref_connector import CrossrefConnector
from src.services.research_engine.connectors.openalex_connector import OpenAlexConnector
from src.services.research_engine.connectors.pubmed_connector import PubMedConnector
from src.services.research_engine.connectors.rag_store_connector import (
    RagStoreConnector,
)
from src.services.research_engine.connectors.semantic_scholar_connector import (
    SemanticScholarConnector,
)


@dataclass(frozen=True)
class ConnectorCapability:
    """Publicly projectable behavior for one canonical connector."""

    connector_id: str
    label: str
    daily_brief_eligible: bool
    full_text: bool
    date_filter: bool
    cursor: bool
    aliases: tuple[str, ...] = ()


CONNECTOR_CAPABILITIES = (
    ConnectorCapability("arxiv", "arXiv", True, False, False, False),
    ConnectorCapability("crossref", "Crossref", True, False, False, False),
    ConnectorCapability("openalex", "OpenAlex", True, False, False, True),
    ConnectorCapability("pubmed", "PubMed", True, False, False, False),
    ConnectorCapability("rag_store", "Workspace documents", False, True, False, False),
    ConnectorCapability(
        "semantic_scholar",
        "Semantic Scholar",
        True,
        False,
        False,
        False,
        ("web",),
    ),
)

_CAPABILITIES_BY_ID = {
    capability.connector_id: capability for capability in CONNECTOR_CAPABILITIES
}
_ALIASES = {
    alias: capability.connector_id
    for capability in CONNECTOR_CAPABILITIES
    for alias in capability.aliases
}


def safe_capability_projection() -> list[dict[str, object]]:
    """Return only the non-sensitive connector fields exposed by the API."""
    return [
        {
            "id": capability.connector_id,
            "label": capability.label,
            "daily_brief_eligible": capability.daily_brief_eligible,
            "available": True,
            "features": {
                "full_text": capability.full_text,
                "date_filter": capability.date_filter,
                "cursor": capability.cursor,
            },
        }
        for capability in sorted(
            CONNECTOR_CAPABILITIES, key=lambda item: item.connector_id
        )
    ]


def normalize_connector_selection(
    ids: Sequence[str], *, daily_brief_only: bool
) -> tuple[str, ...]:
    """Validate connector IDs and normalize permitted legacy aliases."""
    if daily_brief_only and not 1 <= len(ids) <= 4:
        raise ValueError("Daily Research Brief requires between 1 and 4 providers")
    if not ids:
        raise ValueError("Select at least one research connector")

    normalized: list[str] = []
    for connector_id in ids:
        if connector_id in _ALIASES:
            canonical_id = _ALIASES[connector_id]
            if canonical_id in normalized:
                raise ValueError(f"duplicate research connector: {canonical_id}")
            if daily_brief_only:
                raise ValueError(
                    f"Connector alias '{connector_id}' is not selectable for Daily Research Brief"
                )
        else:
            canonical_id = connector_id

        capability = _CAPABILITIES_BY_ID.get(canonical_id)
        if capability is None:
            raise ValueError(f"unknown research connector: {connector_id}")
        if canonical_id in normalized:
            raise ValueError(f"duplicate research connector: {canonical_id}")
        if daily_brief_only and not capability.daily_brief_eligible:
            raise ValueError(
                f"Connector '{canonical_id}' is not eligible for Daily Research Brief"
            )
        normalized.append(canonical_id)
    return tuple(normalized)


def build_connectors(
    rag_search: Callable[..., object],
    connector_ids: Sequence[str] | None = None,
) -> dict[str, SourceConnector]:
    """Build the legacy registry or an explicit canonical connector subset."""
    selected = (
        tuple(capability.connector_id for capability in CONNECTOR_CAPABILITIES)
        if connector_ids is None
        else normalize_connector_selection(connector_ids, daily_brief_only=False)
    )
    connectors: dict[str, SourceConnector] = {}
    for connector_id in selected:
        if connector_id == "arxiv":
            connectors[connector_id] = ArxivConnector()
        elif connector_id == "crossref":
            connectors[connector_id] = CrossrefConnector(
                mailto=settings.CROSSREF_MAILTO
            )
        elif connector_id == "openalex":
            connectors[connector_id] = OpenAlexConnector(
                api_key=settings.OPENALEX_API_KEY
            )
        elif connector_id == "pubmed":
            connectors[connector_id] = PubMedConnector(api_key=settings.NCBI_API_KEY)
        elif connector_id == "rag_store":
            connectors[connector_id] = RagStoreConnector(search_fn=rag_search)
        elif connector_id == "semantic_scholar":
            connectors[connector_id] = SemanticScholarConnector(
                api_key=settings.SEMANTIC_SCHOLAR_API_KEY
            )

    if connector_ids is None:
        connectors["web"] = connectors["semantic_scholar"]
    return connectors
