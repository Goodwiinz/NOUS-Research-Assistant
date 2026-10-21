"""
ArXiv API routes for paper management, extraction, and knowledge graph integration
"""

from .arxiv_bulk import router as arxiv_bulk_router
from .arxiv_change_tracking import router as arxiv_change_router
from .arxiv_extraction import router as arxiv_extraction_router
from .arxiv_knowledge_graph import router as arxiv_kg_router
from .arxiv_llm_bulk import router as arxiv_llm_bulk_router
from .arxiv_local import router as arxiv_local_router
from .core import router as arxiv_router
from .papers import router as arxiv_papers_router

__all__ = [
    "arxiv_router",
    "arxiv_papers_router",
    "arxiv_kg_router",
    "arxiv_change_router",
    "arxiv_extraction_router",
    "arxiv_local_router",
    "arxiv_bulk_router",
    "arxiv_llm_bulk_router",
]
