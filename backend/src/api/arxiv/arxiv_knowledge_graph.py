"""
ArXiv Knowledge Graph API endpoints

Only /bulk-ingest survives. The subgraph/author-network/analyze-trends/entity/
path/stats endpoints were deleted in the R5-H1 fix: they were unreachable
(zero callers anywhere) and could never return data — `get_kg_integration`
handed out a bare `ArXivKnowledgeGraphIntegration()` whose `kg_service` is only
wired up in `__aenter__`, and three of them called `get_entity_details` /
`find_shortest_path` / `get_graph_statistics`, which have never existed on
`KnowledgeGraphService`.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.dependencies import get_current_user
from src.services.arxiv.arxiv_kg_integration import ArXivKnowledgeGraphIntegration
from src.services.arxiv.arxiv_service import ArXivIngestionService
from src.services.arxiv.persistence import persist_arxiv_documents
from src.services.expensive_work_admission import metered_expensive_work

logger = logging.getLogger(__name__)
router = APIRouter()


class BulkIngestionRequest(BaseModel):
    query: str = Field(..., description="Search query for papers")
    # Bounded: an unbounded max_results fed straight into arXiv ingestion.
    max_results: int = Field(
        default=100, ge=1, le=500, description="Maximum papers to process"
    )
    categories: Optional[List[str]] = Field(
        default=None, description="Filter by categories"
    )
    create_kg_entries: bool = Field(
        default=True, description="Create knowledge graph entries"
    )


# Dependency injection
async def get_kg_integration():
    """Yield an *entered* KG integration, or None when the graph is unavailable.

    `kg_service` is only wired up in `__aenter__`; a bare constructor call
    yields an instance that silently writes nothing (R5-H1). A Neo4j failure
    now raises out of `__aenter__` — document ingestion still works without the
    graph, so it is reported (`kg_status`) rather than 503-ing the whole route.
    """
    integration = ArXivKnowledgeGraphIntegration()
    try:
        await integration.__aenter__()
    except Exception:
        logger.exception("arXiv knowledge graph unavailable; KG writes disabled")
        yield None
        return
    try:
        yield integration
    finally:
        await integration.__aexit__(None, None, None)


async def get_arxiv_service():
    """Get ArXiv service"""
    try:
        return ArXivIngestionService()
    except Exception as e:
        logger.error(f"Failed to initialize ArXiv service: {e}")
        raise HTTPException(status_code=503, detail="ArXiv service unavailable")


@router.post("/bulk-ingest")
@metered_expensive_work
async def bulk_ingest_with_kg(
    request: BulkIngestionRequest,
    current_user: dict = Depends(get_current_user),
    arxiv_service: ArXivIngestionService = Depends(get_arxiv_service),
    kg_integration: Optional[ArXivKnowledgeGraphIntegration] = Depends(
        get_kg_integration
    ),
):
    """
    Bulk ingest arXiv papers with knowledge graph integration

    This endpoint searches for arXiv papers matching the query and optionally
    creates knowledge graph entries for extracted entities and relationships.
    """
    try:
        logger.info(f"Bulk ingesting papers for query: {request.query}")

        # Search for papers
        papers = await arxiv_service.search_papers(
            query=request.query,
            max_results=request.max_results,
            categories=request.categories,
        )

        if not papers:
            return {
                "status": "success",
                "message": "No papers found matching the query",
                "papers_found": 0,
                "kg_entries_created": 0,
            }

        # get_current_user returns a User ORM object (despite the dict
        # annotation), but handle both shapes so the tenant org/user are never
        # silently dropped.
        _org_raw = (
            current_user.get("organization_id")
            if isinstance(current_user, dict)
            else getattr(current_user, "organization_id", None)
        )
        if not _org_raw:
            raise HTTPException(
                status_code=403, detail="User has no organization; cannot ingest"
            )
        paper_org_id = str(_org_raw)
        _user_raw = (
            current_user.get("id")
            if isinstance(current_user, dict)
            else getattr(current_user, "id", None)
        )
        user_id = str(_user_raw) if _user_raw else None

        # Ingest papers. Entity extraction happens separately via the KG
        # integration call below — ingest_papers has no extract_entities param.
        # ingest_papers only builds transient in-memory documents (mirrors
        # core.py's background ingest task) — persist_arxiv_documents is what
        # actually durably writes them, same as the other live caller.
        documents = await arxiv_service.ingest_papers(papers=papers)
        persisted = await persist_arxiv_documents(
            documents, user_id=user_id, organization_id=paper_org_id
        )
        ingested_count = len(persisted.document_ids)

        # Create knowledge graph entries if requested. kg_entries_created counts
        # nodes/edges actually written — it used to count papers *attempted*,
        # reporting N while writing zero.
        kg_entries_count = 0
        if not request.create_kg_entries:
            kg_status = "disabled"
        elif kg_integration is None:
            kg_status = "unavailable"
        else:
            kg_status = "ok"
            for paper in papers:
                try:
                    result = await kg_integration.process_paper_kg_integration(
                        paper, organization_id=paper_org_id
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to process KG for paper {paper.get('id')}: {e}"
                    )
                    continue
                if not result:
                    logger.warning(
                        f"KG integration returned nothing for {paper.get('id')}"
                    )
                    continue
                kg_entries_count += result.get("entities_created", 0) + result.get(
                    "relationships_created", 0
                )

        return {
            "status": "success",
            "query": request.query,
            "papers_found": len(papers),
            "papers_ingested": ingested_count,
            "kg_status": kg_status,
            "kg_entries_created": kg_entries_count,
            "categories_filter": request.categories,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to bulk ingest: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
