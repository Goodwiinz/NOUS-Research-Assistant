"""Source connectors for the research engine."""

from src.services.research_engine.connectors.arxiv_connector import ArxivConnector
from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.connectors.crossref_connector import CrossrefConnector
from src.services.research_engine.connectors.openalex_connector import OpenAlexConnector
from src.services.research_engine.connectors.pubmed_connector import PubMedConnector
from src.services.research_engine.connectors.rag_store_connector import (
    RagStoreConnector,
)
from src.services.research_engine.connectors.registry import (
    CONNECTOR_CAPABILITIES,
    ConnectorCapability,
    build_connectors,
    normalize_connector_selection,
    safe_capability_projection,
)
from src.services.research_engine.connectors.semantic_scholar_connector import (
    SemanticScholarConnector,
)

__all__ = [
    "ArxivConnector",
    "CrossrefConnector",
    "OpenAlexConnector",
    "PubMedConnector",
    "RagStoreConnector",
    "SemanticScholarConnector",
    "SourceConnector",
    "SourceDocument",
    "CONNECTOR_CAPABILITIES",
    "ConnectorCapability",
    "build_connectors",
    "normalize_connector_selection",
    "safe_capability_projection",
]
