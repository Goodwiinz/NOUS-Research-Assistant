"""One provider registry for streamed and background research execution."""

from typing import Any, Callable, Coroutine

from src.core.config import settings
from src.services.research_engine.connectors import (
    ArxivConnector,
    CrossrefConnector,
    PubMedConnector,
    RagStoreConnector,
    SemanticScholarConnector,
    SourceConnector,
)
from src.services.research_engine.connectors.openalex_connector import OpenAlexConnector


def build_connectors(
    rag_search: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
) -> dict[str, SourceConnector]:
    semantic = SemanticScholarConnector(api_key=settings.SEMANTIC_SCHOLAR_API_KEY)
    return {
        "arxiv": ArxivConnector(),
        "semantic_scholar": semantic,
        "crossref": CrossrefConnector(mailto=settings.CROSSREF_MAILTO),
        "pubmed": PubMedConnector(api_key=settings.NCBI_API_KEY),
        "openalex": OpenAlexConnector(api_key=settings.OPENALEX_API_KEY),
        "web": semantic,  # Legacy alias; returned provenance names the real provider.
        "rag_store": RagStoreConnector(search_fn=rag_search),
    }
