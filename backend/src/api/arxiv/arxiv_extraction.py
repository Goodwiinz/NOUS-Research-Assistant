"""
ArXiv Paper Feature Extraction API endpoints
"""

import asyncio
import inspect
import logging
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.arxiv.arxiv_kg_integration import ArXivKnowledgeGraphIntegration
from src.services.arxiv.arxiv_service import ArXivIngestionService
from src.services.expensive_work_admission import require_expensive_work_admission
from src.services.processing.entity_extraction_service import EntityExtractionService

logger = logging.getLogger(__name__)
router = APIRouter()


class ExtractionRequest(BaseModel):
    paper_ids: List[str] = Field(
        ...,
        description="List of ArXiv paper IDs to extract features from",
        min_length=1,
        max_length=50,
    )
    extract_entities: bool = Field(
        default=True, description="Extract entities and relationships"
    )
    extract_topics: bool = Field(default=True, description="Extract topics and themes")
    extract_citations: bool = Field(
        default=True, description="Extract citation information"
    )
    extract_keyphrases: bool = Field(default=True, description="Extract key phrases")
    extract_summaries: bool = Field(default=True, description="Generate summaries")
    update_knowledge_graph: bool = Field(
        default=True, description="Update knowledge graph with extracted information"
    )


class ExtractionResponse(BaseModel):
    status: str
    message: str
    processed_count: int
    failed_count: int = 0
    results: List[Dict[str, Any]]


def _get_organization_id(current_user: User) -> str:
    """Resolve organization ID from the authenticated User.

    ``get_current_user`` returns a ``User`` ORM object (organization eagerly
    loaded), NOT a dict — so this reads attributes, never ``.get()``.

    Never fabricate an org id: a random UUID is an invalid FK and silently
    defeats tenant isolation. Raise 403 instead.
    """
    organization_id = current_user.organization_id or (
        current_user.organization.id if current_user.organization else None
    )
    if not organization_id:
        raise HTTPException(
            status_code=403,
            detail="No organization associated with this account",
        )
    return str(organization_id)


def _serialize_entities(entities: List[Any]) -> Dict[str, Any]:
    """Convert SQLAlchemy entity models into API-safe dictionaries."""
    serialized: List[Dict[str, Any]] = []

    for entity in entities:
        if hasattr(entity, "to_dict"):
            entity_dict = entity.to_dict()
        elif isinstance(entity, dict):
            entity_dict = entity
        else:
            continue

        serialized.append(
            {
                "id": entity_dict.get("id"),
                "name": entity_dict.get("name", ""),
                "type": (entity_dict.get("entity_type") or "OTHER").upper(),
                "confidence": entity_dict.get("confidence", 0.0),
                "properties": entity_dict.get("properties") or {},
            }
        )

    return {"entities": serialized, "relationships": [], "count": len(serialized)}


@router.post(
    "/extract-features", dependencies=[Depends(require_expensive_work_admission)]
)
async def extract_paper_features(
    request: ExtractionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """
    Extract features from ArXiv papers

    This endpoint extracts various features from ArXiv papers including:
    - Entities and relationships
    - Topics and themes
    - Citation information
    - Key phrases
    - Summaries

    The extraction can be performed synchronously or asynchronously in the background.
    """
    try:
        logger.info(f"Starting feature extraction for {len(request.paper_ids)} papers")

        organization_id = _get_organization_id(current_user)
        entity_service = None
        entity_service_error = None

        if request.extract_entities:
            try:
                entity_service = EntityExtractionService()
            except Exception as entity_error:
                entity_service_error = str(entity_error)
                logger.error(
                    f"Failed to initialize entity extraction service: {entity_error}"
                )

        # Initialize services
        async with ArXivIngestionService() as arxiv_service:
            extraction_results = []
            processed_count = 0
            failed_count = 0

            for paper_id in request.paper_ids:
                try:
                    # Get paper details
                    papers = await arxiv_service.search_papers(
                        query=f"id:{paper_id}", max_results=1
                    )

                    if not papers:
                        logger.warning(f"Paper {paper_id} not found")
                        extraction_results.append(
                            {
                                "paper_id": paper_id,
                                "extraction_status": "failed",
                                "error": "Paper not found in arXiv index",
                                "features": {},
                            }
                        )
                        continue

                    paper = papers[0]
                    extraction_result = {
                        "paper_id": paper_id,
                        "title": paper.get("title", ""),
                        "extraction_status": "processing",
                        "features": {},
                    }

                    # Extract text content
                    text_content = paper.get("abstract", "")
                    if paper.get("content_text"):
                        text_content += "\n" + paper.get("content_text", "")

                    # Extract entities
                    if request.extract_entities:
                        if entity_service_error:
                            extraction_result["features"]["entities"] = {
                                "error": entity_service_error
                            }
                        elif not text_content:
                            extraction_result["features"]["entities"] = {
                                "error": "No text available for entity extraction"
                            }
                        else:
                            try:
                                # EntityExtractionService only needs document id + organization_id.
                                document_stub = SimpleNamespace(
                                    id=str(uuid.uuid4()),
                                    organization_id=organization_id,
                                )

                                entities = entity_service.extract_entities_from_text(
                                    document=document_stub, text=text_content
                                )
                                extraction_result["features"]["entities"] = (
                                    _serialize_entities(entities)
                                )
                            except Exception as e:
                                logger.error(
                                    f"Entity extraction failed for {paper_id}: {e}"
                                )
                                extraction_result["features"]["entities"] = {
                                    "error": str(e)
                                }

                    # Extract topics using LLM
                    if request.extract_topics and text_content:
                        try:
                            topics = await _extract_topics_with_llm(text_content)
                            if topics:
                                extraction_result["features"]["topics"] = topics
                            else:
                                # Real topic extraction is not yet implemented —
                                # surface it honestly instead of fabricating.
                                extraction_result["features"]["topics"] = {
                                    "unsupported": (
                                        "Topic extraction is not yet available"
                                    ),
                                    "topics": [],
                                }
                        except Exception as e:
                            logger.error(f"Topic extraction failed for {paper_id}: {e}")
                            extraction_result["features"]["topics"] = {"error": str(e)}

                    # Extract key phrases
                    if request.extract_keyphrases and text_content:
                        try:
                            keyphrases = await _extract_keyphrases(text_content)
                            extraction_result["features"]["keyphrases"] = keyphrases
                        except Exception as e:
                            logger.error(
                                f"Keyphrase extraction failed for {paper_id}: {e}"
                            )
                            extraction_result["features"]["keyphrases"] = {
                                "error": str(e)
                            }

                    # Generate summary
                    if request.extract_summaries and text_content:
                        try:
                            summary = await _generate_summary(
                                text_content, paper.get("title", "")
                            )
                            extraction_result["features"]["summary"] = summary
                        except Exception as e:
                            logger.error(
                                f"Summary generation failed for {paper_id}: {e}"
                            )
                            extraction_result["features"]["summary"] = {"error": str(e)}

                    # Extract citations. NOTE: the arXiv metadata parser does not
                    # populate citations/references, so these are effectively
                    # always empty. Report the feature as unsupported rather than
                    # implying a real (empty) citation graph was extracted.
                    if request.extract_citations:
                        citations = paper.get("citations", []) or []
                        references = paper.get("references", []) or []
                        citations_feature = {
                            "citations": citations,
                            "references": references,
                            "citation_count": len(citations),
                        }
                        if not citations and not references:
                            citations_feature["unsupported"] = (
                                "Citation extraction is not available for arXiv "
                                "metadata"
                            )
                        extraction_result["features"]["citations"] = citations_feature

                    # Per-feature failure honesty: a paper is only "completed"
                    # if every attempted feature succeeded. Any feature carrying
                    # an {"error": ...} payload downgrades the paper to "partial"
                    # (some features failed) or "failed" (all did). The
                    # per-feature error stays in the payload so the client can
                    # render exactly what broke.
                    features = extraction_result["features"]
                    attempted = list(features.keys())
                    errored = [
                        name
                        for name, val in features.items()
                        if isinstance(val, dict) and "error" in val
                    ]
                    if attempted and len(errored) == len(attempted):
                        extraction_result["extraction_status"] = "failed"
                    elif errored:
                        extraction_result["extraction_status"] = "partial"
                    else:
                        extraction_result["extraction_status"] = "completed"

                    if errored:
                        extraction_result["errors"] = errored

                    extraction_results.append(extraction_result)

                    if extraction_result["extraction_status"] == "failed":
                        failed_count += 1
                    else:
                        processed_count += 1

                except Exception as e:
                    logger.error(f"Failed to process paper {paper_id}: {e}")
                    failed_count += 1
                    extraction_results.append(
                        {
                            "paper_id": paper_id,
                            "extraction_status": "failed",
                            "error": str(e),
                            "features": {},
                        }
                    )

            # Update knowledge graph if requested
            if request.update_knowledge_graph and processed_count > 0:
                if background_tasks:
                    background_tasks.add_task(
                        _update_knowledge_graph_with_extractions,
                        extraction_results,
                        organization_id,
                    )
                else:
                    await _update_knowledge_graph_with_extractions(
                        extraction_results, organization_id
                    )

            return ExtractionResponse(
                status="success",
                message=(
                    f"Extracted features from {processed_count} papers"
                    + (f" ({failed_count} failed)" if failed_count else "")
                ),
                processed_count=processed_count,
                failed_count=failed_count,
                results=extraction_results,
            )

    except Exception as e:
        logger.error(f"Feature extraction failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/extracted-features")
async def get_extracted_features(
    paper_id: Optional[str] = Query(
        None, description="Get features for specific paper"
    ),
    limit: int = Query(default=50, description="Maximum number of papers to return"),
    current_user: User = Depends(get_current_user),
):
    """
    Get previously extracted features for papers
    """
    try:
        # Query database for extracted features
        from sqlalchemy import select

        from src.core.database import get_db_session
        from src.models.document import Document

        extracted_features = []

        async with get_db_session() as db:
            # Build query — scoped to the caller's org and non-deleted docs.
            # Previously unscoped, returning every org's extracted features.
            # The arXiv id is stored in the document_metadata JSON under
            # "arxiv_id" (Document has no external_id column), so filter on that.
            stmt = select(Document).where(
                Document.document_metadata.isnot(None),
                Document.organization_id == current_user.organization_id,
                Document.is_deleted == False,
            )

            if paper_id:
                # .as_string(), NOT .astext — generic sqlalchemy.JSON has no
                # astext comparator; it raises AttributeError at build time.
                stmt = stmt.where(
                    Document.document_metadata["arxiv_id"].as_string() == paper_id
                )

            stmt = stmt.limit(limit)

            result = await db.execute(stmt)
            documents = result.scalars().all()

            for doc in documents:
                metadata = doc.document_metadata or {}
                if "extracted_features" not in metadata:
                    continue

                extracted_features.append(
                    {
                        "paper_id": metadata.get("arxiv_id"),
                        "title": doc.title,
                        "features": metadata.get("extracted_features", {}),
                        "extracted_at": metadata.get("features_extracted_at"),
                    }
                )

        return {
            "status": "success",
            "count": len(extracted_features),
            "features": extracted_features,
        }

    except Exception as e:
        logger.error(f"Failed to get extracted features: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/bulk-extract", dependencies=[Depends(require_expensive_work_admission)])
async def bulk_extract_features(
    request: dict, current_user: User = Depends(get_current_user)
):
    """
    Bulk extract features from recent papers in specified categories
    """
    try:
        # Extract parameters from request body
        categories = request.get("categories", [])
        days_back = request.get("days_back", 7)
        # Only the first 50 are extracted below; never search past that.
        max_papers = min(int(request.get("max_papers", 50)), 50)
        extraction_options = request.get("extraction_options", {})

        logger.info(f"Starting bulk extraction for categories: {categories}")

        # First, get papers from specified categories
        async with ArXivIngestionService() as arxiv_service:
            from datetime import datetime, timedelta

            papers = await arxiv_service.search_papers(
                query=" OR ".join([f"cat:{cat}" for cat in categories]),
                max_results=max_papers,
                date_from=datetime.utcnow() - timedelta(days=days_back),
            )

        if not papers:
            return {
                "status": "success",
                "message": "No papers found in specified categories",
                "paper_count": 0,
            }

        # Extract paper IDs, capped to the per-request maximum (each paper is a
        # sequential ~3s arXiv call inside extract_paper_features).
        paper_ids = [paper["id"] for paper in papers][:50]

        # Create extraction request
        extraction_request = ExtractionRequest(
            paper_ids=paper_ids, **extraction_options
        )

        # Process extraction
        bulk_background_tasks = BackgroundTasks()
        result = await extract_paper_features(
            request=extraction_request,
            background_tasks=bulk_background_tasks,
            current_user=current_user,
        )

        # Execute queued background tasks since this endpoint invokes extraction directly.
        for task in bulk_background_tasks.tasks:
            maybe_awaitable = task.func(*task.args, **task.kwargs)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

        return {
            "status": "success",
            "message": f"Bulk extraction completed for {result.processed_count} papers",
            "categories_searched": categories,
            "papers_found": len(papers),
            "papers_processed": result.processed_count,
            "results": result.results,
        }

    except Exception as e:
        logger.error(f"Bulk extraction failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# Helper functions
async def _extract_topics_with_llm(text: str, max_topics: int = 5) -> List[str]:
    """Extract topics from text using an LLM.

    TODO(arxiv-audit): real LLM-backed topic extraction is not implemented yet.
    Return an empty list rather than hard-coded placeholder topics — fabricated
    topics were previously written to the knowledge graph as if they were real.
    The caller treats an empty result as "unsupported" and writes nothing.
    """
    return []


async def _extract_keyphrases(text: str, max_phrases: int = 10) -> List[str]:
    """Extract key phrases from text"""
    try:
        # Simple keyword extraction - could be enhanced with NLP libraries
        import re

        # Extract noun phrases and important terms
        words = text.lower().split()
        # Filter out common stop words
        stop_words = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "can",
            "this",
            "that",
            "these",
            "those",
        }
        filtered_words = [w for w in words if len(w) > 3 and w not in stop_words]
        # Return top frequent words as key phrases
        from collections import Counter

        word_freq = Counter(filtered_words)
        return [word for word, _ in word_freq.most_common(max_phrases)]
    except Exception:
        return []


async def _generate_summary(text: str, title: str, max_length: int = 200) -> str:
    """Generate a summary of the text"""
    try:
        # Simple extractive summary - take first few sentences
        sentences = text.split(". ")
        summary = ". ".join(sentences[:3])
        if len(summary) > max_length:
            summary = summary[:max_length] + "..."
        return summary
    except Exception:
        return text[:max_length] + "..." if len(text) > max_length else text


async def _update_knowledge_graph_with_extractions(
    extraction_results: List[Dict[str, Any]], organization_id: Optional[str] = None
):
    """Background task to update knowledge graph with extracted features"""
    try:
        from src.models.graph import (
            CreateEntityRequest,
            CreateRelationshipRequest,
            EntityType,
            ExtractionMethod,
            RelationshipType,
        )

        async with ArXivKnowledgeGraphIntegration() as kg:
            synced_count = 0

            for result in extraction_results:
                # Sync both fully-completed and partially-completed papers so a
                # single failed feature does not discard the good ones. Only
                # truly failed / unprocessed papers are skipped.
                if result.get("extraction_status") not in ("completed", "partial"):
                    continue

                paper_id = result.get("paper_id")
                features = result.get("features") or {}

                try:
                    doc_entity_request = CreateEntityRequest(
                        entity_type=EntityType.DOCUMENT,
                        name=result.get("title", f"ArXiv Paper: {paper_id}"),
                        confidence_score=0.9,
                        extraction_method=ExtractionMethod.SPACY_NER,
                        metadata={"paper_id": paper_id, "source": "arxiv_extraction"},
                        organization_id=organization_id,
                    )
                    doc_entity = await asyncio.to_thread(
                        kg.kg_service.create_entity, doc_entity_request
                    )

                    if not doc_entity:
                        logger.error(f"Failed to create document entity for {paper_id}")
                        continue

                    entities_payload = features.get("entities")
                    if isinstance(entities_payload, dict):
                        for entity in entities_payload.get("entities", []):
                            raw_entity_type = str(entity.get("type") or "OTHER").upper()
                            if raw_entity_type == "CUSTOM":
                                raw_entity_type = "OTHER"
                            mapped_entity_type = EntityType.__members__.get(
                                raw_entity_type, EntityType.OTHER
                            )

                            entity_request = CreateEntityRequest(
                                entity_type=mapped_entity_type,
                                name=entity.get("name", ""),
                                confidence_score=float(
                                    entity.get("confidence")
                                    or entity.get("properties", {}).get(
                                        "confidence", 0.5
                                    )
                                ),
                                extraction_method=ExtractionMethod.SPACY_NER,
                                metadata=entity.get("properties", {}),
                                organization_id=organization_id,
                            )
                            await asyncio.to_thread(
                                kg.kg_service.create_entity, entity_request
                            )

                    if isinstance(features.get("topics"), list):
                        for topic in features["topics"]:
                            # Skip empty/placeholder topics — never write blanks
                            # to the KG.
                            if not topic or not str(topic).strip():
                                continue
                            topic_request = CreateEntityRequest(
                                entity_type=EntityType.CONCEPT,
                                name=topic,
                                confidence_score=0.8,
                                extraction_method=ExtractionMethod.SPACY_NER,
                                metadata={
                                    "source": "arxiv_extraction",
                                    "paper_id": paper_id,
                                },
                                organization_id=organization_id,
                            )
                            await asyncio.to_thread(
                                kg.kg_service.create_entity, topic_request
                            )

                    if isinstance(features.get("keyphrases"), list):
                        for keyphrase in features["keyphrases"]:
                            kp_request = CreateEntityRequest(
                                entity_type=EntityType.CONCEPT,
                                name=keyphrase,
                                confidence_score=0.7,
                                extraction_method=ExtractionMethod.SPACY_NER,
                                metadata={
                                    "source": "arxiv_keyphrase",
                                    "paper_id": paper_id,
                                },
                                organization_id=organization_id,
                            )
                            await asyncio.to_thread(
                                kg.kg_service.create_entity, kp_request
                            )

                    relationships = []
                    if isinstance(entities_payload, dict):
                        relationships = entities_payload.get("relationships", [])

                    for rel in relationships:
                        source_name = (
                            rel.get("source") or rel.get("source_entity") or ""
                        )
                        target_name = (
                            rel.get("target") or rel.get("target_entity") or ""
                        )

                        if not source_name or not target_name:
                            continue

                        source_entities = await asyncio.to_thread(
                            kg.kg_service.search_entities,
                            query=source_name,
                            limit=1,
                            organization_id=organization_id,
                        )
                        target_entities = await asyncio.to_thread(
                            kg.kg_service.search_entities,
                            query=target_name,
                            limit=1,
                            organization_id=organization_id,
                        )

                        if source_entities and target_entities:
                            rel_request = CreateRelationshipRequest(
                                source_entity_id=source_entities[0].id,
                                target_entity_id=target_entities[0].id,
                                relationship_type=RelationshipType.RELATED_TO,
                                confidence_score=float(
                                    rel.get("properties", {}).get("confidence", 0.5)
                                ),
                                metadata=rel.get("properties", {}),
                                organization_id=organization_id,
                            )
                            await asyncio.to_thread(
                                kg.kg_service.create_relationship, rel_request
                            )

                    synced_count += 1
                except Exception as paper_error:
                    logger.error(
                        f"Knowledge graph update failed for paper {paper_id}: {paper_error}"
                    )
                    continue

        logger.info(f"Knowledge graph updated for {synced_count} papers")

    except Exception as e:
        logger.error(f"Failed to update knowledge graph: {e}")
