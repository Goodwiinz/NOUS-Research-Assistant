"""
ArXiv Local PDF Processing API endpoints
For processing PDF files stored locally in the data/arxiv directory
"""

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.dependencies import get_current_user
from src.services.processing.entity_extraction_service import EntityExtractionService
from src.services.processing.multimodal_processing_service import (
    MultimodalProcessingService,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Path to local ArXiv PDFs
docker_path = Path("/app/data/arxiv")
local_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "arxiv"

ARXIV_DATA_PATH = docker_path if docker_path.exists() else local_path
logger.info(f"Using ArXiv data path: {ARXIV_DATA_PATH}")


class LocalExtractionRequest(BaseModel):
    paper_ids: Optional[List[str]] = Field(
        None, description="List of paper IDs to process (if None, process all)"
    )
    extract_entities: bool = Field(
        default=True, description="Extract entities and relationships"
    )
    extract_topics: bool = Field(default=True, description="Extract topics and themes")
    extract_keyphrases: bool = Field(default=True, description="Extract key phrases")
    extract_summaries: bool = Field(default=True, description="Generate summaries")
    process_full_content: bool = Field(
        default=True, description="Process full PDF content (not just metadata)"
    )
    update_knowledge_graph: bool = Field(
        default=True, description="Update knowledge graph with extracted information"
    )


class LocalExtractionResponse(BaseModel):
    status: str
    message: str
    total_files_found: int
    processed_count: int
    results: List[Dict[str, Any]]


from src.models.vector import EmbeddingRequest  # noqa: E402
from src.services.embedding.embedding_service import EmbeddingService  # noqa: E402
from src.services.expensive_work_admission import metered_expensive_work

_shared_embedding_service = None


def _get_shared_embedding_service():
    """Process-wide EmbeddingService (R6-M4: no per-document reloads)."""
    global _shared_embedding_service
    if _shared_embedding_service is None:
        _shared_embedding_service = EmbeddingService(lazy=False)
    return _shared_embedding_service


@router.get("/local-papers")
async def list_local_papers(
    limit: int = Query(default=50, description="Maximum number of papers to return"),
    offset: int = Query(default=0, description="Offset for pagination"),
    search: Optional[str] = Query(None, description="Search term to filter papers"),
    current_user: dict = Depends(get_current_user),
):
    """
    List ArXiv papers stored locally in the data/arxiv directory
    """
    try:
        # Get all PDF files
        pdf_files = list(ARXIV_DATA_PATH.glob("*.pdf"))

        # Apply search filter if provided
        if search:
            pdf_files = [f for f in pdf_files if search.lower() in f.name.lower()]

        # Sort by modification time (newest first)
        pdf_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        # Apply pagination
        total_count = len(pdf_files)
        pdf_files = pdf_files[offset : offset + limit]

        papers = []
        for pdf_file in pdf_files:
            # Extract paper ID from filename
            paper_id = pdf_file.stem  # e.g., "2512.12345v1" -> "2512.12345v1"

            # Get file info
            stat = pdf_file.stat()

            papers.append(
                {
                    "paper_id": paper_id,
                    "filename": pdf_file.name,
                    "file_path": str(pdf_file),
                    "file_size": stat.st_size,
                    "modified_date": stat.st_mtime,
                    "download_date": stat.st_mtime,  # Same as modified for our use case
                }
            )

        return {"status": "success", "total_count": total_count, "papers": papers}

    except Exception as e:
        logger.error(f"Failed to list local papers: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/extract-local-features")
@metered_expensive_work
async def extract_features_from_local_pdfs(
    request: LocalExtractionRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """
    Extract features from locally stored ArXiv PDF files

    This endpoint processes PDF files directly from the data/arxiv directory
    and extracts text, entities, topics, and other features.
    """
    try:
        logger.info(f"Starting local PDF feature extraction")

        # Get all PDF files
        pdf_files = list(ARXIV_DATA_PATH.glob("*.pdf"))
        logger.info(f"Found {len(pdf_files)} PDF files")

        # Filter by paper IDs if specified
        if request.paper_ids:
            pdf_files = [
                f for f in pdf_files if any(pid in f.name for pid in request.paper_ids)
            ]

        if not pdf_files:
            logger.warning("No PDF files found matching the criteria!")
            return LocalExtractionResponse(
                status="success",
                message="No PDF files found matching the criteria",
                total_files_found=0,
                processed_count=0,
                results=[],
            )

        # Process PDF files
        extraction_results = []
        processed_count = 0

        for idx, pdf_file in enumerate(pdf_files[:50]):  # Limit to 50 files at once
            try:
                paper_id = pdf_file.stem
                logger.info(f"Processing PDF {idx+1}/{len(pdf_files)}: {paper_id}")
                logger.info(f"File size: {pdf_file.stat().st_size} bytes")

                extraction_result = {
                    "paper_id": paper_id,
                    "filename": pdf_file.name,
                    "file_path": str(pdf_file),
                    "extraction_status": "processing",
                    "features": {},
                }

                # Process PDF with direct PyMuPDF extraction (more reliable than multimodal service)
                if request.process_full_content:
                    logger.info(f"Processing full content for {pdf_file.name}...")
                    try:
                        # Use PyMuPDF (fitz) directly for PDF extraction
                        import fitz  # PyMuPDF

                        pdf_document = fitz.open(str(pdf_file))

                        # Extract text from all pages
                        text_content = []
                        for page_num in range(len(pdf_document)):
                            page = pdf_document.load_page(page_num)
                            text_content.append(page.get_text())

                        extracted_text = "\n".join(text_content)

                        # Extract metadata
                        metadata = pdf_document.metadata
                        pdf_metadata = {
                            "title": (
                                metadata.get("title", "")
                                if metadata.get("title")
                                else ""
                            ),
                            "author": (
                                metadata.get("author", "")
                                if metadata.get("author")
                                else ""
                            ),
                            "subject": (
                                metadata.get("subject", "")
                                if metadata.get("subject")
                                else ""
                            ),
                            "creator": (
                                metadata.get("creator", "")
                                if metadata.get("creator")
                                else ""
                            ),
                            "producer": (
                                metadata.get("producer", "")
                                if metadata.get("producer")
                                else ""
                            ),
                            "creation_date": (
                                metadata.get("creationDate", "")
                                if metadata.get("creationDate")
                                else ""
                            ),
                            "modification_date": (
                                metadata.get("modDate", "")
                                if metadata.get("modDate")
                                else ""
                            ),
                            "page_count": len(pdf_document),
                        }

                        pdf_document.close()

                        extraction_result["features"]["extracted_text"] = extracted_text
                        extraction_result["features"]["metadata"] = pdf_metadata

                        # Log extraction details
                        logger.info(
                            f"Successfully extracted {len(extracted_text)} characters from {pdf_file.name}"
                        )
                        logger.info(
                            f"PDF metadata - Title: {pdf_metadata.get('title', 'N/A')}, Pages: {pdf_metadata.get('page_count', 0)}"
                        )

                        # Check if we got meaningful text
                        if len(extracted_text) < 100:
                            logger.warning(
                                f"Extracted text seems too short ({len(extracted_text)} chars) for {pdf_file.name}"
                            )

                    except ImportError:
                        logger.error(
                            "PyMuPDF (fitz) not available - install with: pip install PyMuPDF"
                        )
                        extraction_result["features"]["extracted_text"] = ""
                        extraction_result["features"]["metadata"] = {}
                    except Exception as e:
                        logger.error(f"Failed to extract PDF content: {e}")
                        import traceback

                        logger.error(traceback.format_exc())
                        extraction_result["features"]["extracted_text"] = ""
                        extraction_result["features"]["metadata"] = {}
                else:
                    logger.warning(
                        f"Full content processing disabled for {pdf_file.name}"
                    )

                # Get extracted text once
                extracted_text = extraction_result["features"].get("extracted_text", "")

                # Extract topics from actual text if available
                if request.extract_topics:
                    if extracted_text:
                        topics = await _extract_topics_from_text(extracted_text)
                    else:
                        # Fallback to filename
                        topics = await _extract_topics_from_filename(pdf_file.name)
                    extraction_result["features"]["topics"] = topics

                # Extract key phrases from actual text if available
                if request.extract_keyphrases:
                    if extracted_text:
                        keyphrases = await _extract_keyphrases_from_text(extracted_text)
                    else:
                        # Fallback to filename
                        keyphrases = _extract_keyphrases_from_filename(pdf_file.name)
                    extraction_result["features"]["keyphrases"] = keyphrases

                # Generate summary from extracted text
                if request.extract_summaries:
                    if extracted_text:
                        # Use first paragraph or first 500 characters as summary
                        summary = (
                            extracted_text[:500] + "..."
                            if len(extracted_text) > 500
                            else extracted_text
                        )
                    else:
                        summary = f"Summary of paper {paper_id}: This paper appears to be from the ArXiv repository."
                    extraction_result["features"]["summary"] = summary

                extraction_result["extraction_status"] = "completed"
                extraction_results.append(extraction_result)
                processed_count += 1

            except Exception as e:
                logger.error(f"Failed to process {pdf_file}: {e}")
                extraction_results.append(
                    {
                        "paper_id": pdf_file.stem,
                        "filename": pdf_file.name,
                        "extraction_status": "failed",
                        "error": str(e),
                    }
                )

        if request.update_knowledge_graph and processed_count > 0:
            logger.info(
                f"Scheduling knowledge graph update for {processed_count} papers"
            )
            # Resolve the caller's org once and thread it into the background KG
            # task so entities created off the request thread are tenant-scoped
            # (the background task gets no current_user otherwise).
            _org_raw = (
                current_user.get("organization_id")
                if isinstance(current_user, dict)
                else getattr(current_user, "organization_id", None)
            )
            org_id = str(_org_raw) if _org_raw else None
            # Create background tasks for each result to process independently
            for result in extraction_results:
                background_tasks.add_task(
                    _post_process_extraction, result, request, org_id
                )

        return LocalExtractionResponse(
            status="success",
            message=f"Successfully processed {processed_count} files",
            total_files_found=len(pdf_files),
            processed_count=processed_count,
            results=extraction_results,
        )

    except Exception as e:
        logger.error(f"Error in local PDF extraction: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Failed to extract features from local PDFs: {str(e)}",
        )


# Global executor for KG updates to prevent resource exhaustion
# Max workers = 5 allows parallel KG updates while preventing resource exhaustion
KG_UPDATE_EXECUTOR = ThreadPoolExecutor(max_workers=5)


async def _post_process_extraction(result, request, organization_id=None):
    """
    Handle async post-processing steps (embeddings, KG update)
    """
    paper_id = result.get("paper_id", "unknown")
    logger.info(f"Starting post-processing for paper: {paper_id}")

    try:
        extracted_text = result["features"].get("extracted_text", "")

        # 1. Generate Embeddings (Async)
        if extracted_text and len(extracted_text) > 100:
            try:
                from src.services.embedding.embedding_service import EmbeddingService

                # R6-M4: one shared instance (fresh-per-paper reloaded the
                # SentenceTransformer weights from disk every document); the
                # azure_openai branch was dead — a fresh instance's provider
                # is always the default.
                embedding = await _get_shared_embedding_service().generate_embedding(
                    EmbeddingRequest(
                        text=extracted_text[:2000],
                        model="sentence-transformers/all-MiniLM-L6-v2",
                    )
                )

                result["features"]["embedding"] = embedding
                logger.info(
                    f"Generated embedding with {len(embedding.embedding) if hasattr(embedding, 'embedding') else 'unknown'} dimensions"
                )
            except Exception as e:
                logger.error(f"Error generating embedding: {e}")

        # 2. Extract Topics (Async)
        if request.extract_topics and extracted_text:
            pass

        # 3. Update Knowledge Graph (Sync wrapper in ThreadPool)
        if request.update_knowledge_graph:
            import asyncio

            loop = asyncio.get_event_loop()

            # Run the synchronous KG update in the global executor
            try:
                logger.info(
                    f"Submitting KG update for {result.get('paper_id', 'unknown')} to executor"
                )
                await loop.run_in_executor(
                    KG_UPDATE_EXECUTOR,
                    _update_knowledge_graph_with_local_extractions_sync,
                    result,
                    organization_id,
                )
                logger.info(
                    f"KG update task submitted for {result.get('paper_id', 'unknown')}"
                )
            except Exception as e:
                logger.error(f"Failed to update knowledge graph: {e}")
                import traceback

                logger.error(traceback.format_exc())

    except Exception as e:
        logger.error(f"Error in post-processing: {e}")


def _update_knowledge_graph_with_local_extractions_sync(
    result: Dict[str, Any], organization_id: Optional[str] = None
):
    """
    Synchronous wrapper for knowledge graph updates
    """
    try:
        from src.models.graph import CreateEntityRequest, EntityType, ExtractionMethod
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        kg_service = KnowledgeGraphService()

        # Extract metadata
        features = result.get("features", {})
        metadata = features.get("metadata", {})
        paper_id = result.get("paper_id", "")
        filename = result.get("filename", "")

        # Helper to safely get string values
        def get_safe_str(val):
            return str(val) if val else ""

        # Create Paper Entity
        paper_title = get_safe_str(metadata.get("title", ""))

        paper_entity_request = CreateEntityRequest(
            entity_type=EntityType.DOCUMENT,
            name=(
                paper_title
                if paper_title and paper_title != "pdf"
                else f"ArXiv Paper: {paper_id}"
            ),
            confidence_score=0.9,
            extraction_method=ExtractionMethod.SPACY_NER,
            metadata={
                "paper_id": paper_id,
                "filename": filename,
                "title": paper_title,
                "author": get_safe_str(metadata.get("author", "")),
                "subject": get_safe_str(metadata.get("subject", "")),
                "creator": get_safe_str(metadata.get("creator", "")),
                "source": "local_arxiv",
                "topics": features.get("topics", []),
                "keyphrases": features.get("keyphrases", []),
                "summary": features.get("summary", ""),
                "extracted_at": datetime.now().isoformat(),
            },
            organization_id=organization_id,
        )

        logger.info(f"Creating paper entity for {paper_id}...")
        paper_entity = kg_service.create_entity(paper_entity_request)
        logger.info(
            f"Created/Updated paper entity: {paper_entity.id if paper_entity else 'None'}"
        )

        if not paper_entity:
            logger.warning(f"Failed to create paper entity for {paper_id}")
            return

        # Add topics as entities and create relationships
        topics = features.get("topics", [])
        for topic in topics:
            if not topic:
                continue

            try:
                # Create or get topic entity
                topic_entity_request = CreateEntityRequest(
                    entity_type=EntityType.CONCEPT,
                    name=topic,
                    confidence_score=0.8,
                    extraction_method=ExtractionMethod.SPACY_NER,
                    metadata={"source": "arxiv_extraction", "paper_id": paper_id},
                    organization_id=organization_id,
                )
                topic_entity = kg_service.create_entity(topic_entity_request)

                # Create relationship: PAPER -> HAS_TOPIC -> TOPIC
                if topic_entity:
                    from src.models.graph import (
                        CreateRelationshipRequest,
                        RelationshipType,
                    )

                    logger.info(
                        f"Creating relationship between paper {paper_entity.id} and topic {topic_entity.id}"
                    )
                    rel_request = CreateRelationshipRequest(
                        source_entity_id=paper_entity.id,
                        target_entity_id=topic_entity.id,
                        relationship_type=RelationshipType.RELATED_TO,
                        confidence_score=0.85,
                        metadata={"type": "topic_extraction"},
                        organization_id=organization_id,
                    )
                    relationship = kg_service.create_relationship(rel_request)
                    if relationship:
                        logger.info(f"✅ Created relationship: {relationship.id}")
                    else:
                        logger.warning(f"❌ Failed to create relationship")
            except Exception as e:
                logger.warning(f"Failed to process topic '{topic}': {e}")
                continue

    except Exception as e:
        logger.error(f"Error in knowledge graph update: {e}")
        raise


@router.get("/local-stats")
async def get_local_papers_stats(current_user: dict = Depends(get_current_user)):
    """
    Get statistics about locally stored ArXiv papers
    """
    try:
        pdf_files = list(ARXIV_DATA_PATH.glob("*.pdf"))

        # Calculate statistics
        total_files = len(pdf_files)
        total_size = sum(f.stat().st_size for f in pdf_files)

        # Group by date ranges
        date_groups = {"last_24h": 0, "last_week": 0, "last_month": 0, "older": 0}

        import time

        now = time.time()
        day_ago = now - 86400
        week_ago = now - 604800
        month_ago = now - 2592000

        for pdf_file in pdf_files:
            mtime = pdf_file.stat().st_mtime
            if mtime > day_ago:
                date_groups["last_24h"] += 1
            elif mtime > week_ago:
                date_groups["last_week"] += 1
            elif mtime > month_ago:
                date_groups["last_month"] += 1
            else:
                date_groups["older"] += 1

        return {
            "status": "success",
            "statistics": {
                "total_files": total_files,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "average_size_mb": (
                    round((total_size / total_files) / (1024 * 1024), 2)
                    if total_files > 0
                    else 0
                ),
                "date_distribution": date_groups,
            },
        }

    except Exception as e:
        logger.error(f"Failed to get local papers stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/process-batch")
@metered_expensive_work
async def process_batch_local_papers(
    background_tasks: BackgroundTasks,
    batch_size: int = Query(
        default=10, description="Number of papers to process in this batch"
    ),
    offset: int = Query(default=0, description="Starting offset"),
    request: dict = Body({}),
    current_user: dict = Depends(get_current_user),
):
    """
    Process a batch of local papers for feature extraction
    Useful for processing papers in chunks to avoid timeouts
    """
    try:
        # Get extraction options from request
        extraction_options = request.get("extraction_options", {})

        # Get PDF files with pagination
        pdf_files = list(ARXIV_DATA_PATH.glob("*.pdf"))
        pdf_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        # Apply offset and limit
        batch_files = pdf_files[offset : offset + batch_size]

        if not batch_files:
            return {
                "status": "success",
                "message": "No more papers to process",
                "processed": 0,
                "offset": offset,
            }

        # Process the batch
        extraction_request = LocalExtractionRequest(
            paper_ids=[f.stem for f in batch_files], **extraction_options
        )

        # Pass the request's BackgroundTasks through (NOT None): when
        # extraction_options enables update_knowledge_graph, the inner handler
        # schedules per-paper KG tasks via background_tasks.add_task — a None
        # here raised AttributeError that the broad except reported as a failed
        # batch, silently dropping the KG update.
        result = await extract_features_from_local_pdfs(
            request=extraction_request,
            background_tasks=background_tasks,
            current_user=current_user,
        )

        return {
            "status": "success",
            "message": f"Processed batch of {result.processed_count} papers",
            "processed": result.processed_count,
            "offset": offset + batch_size,
            "has_more": offset + batch_size < len(pdf_files),
            "results": result.results,
        }

    except Exception as e:
        logger.error(f"Batch processing failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# Helper functions
async def _extract_topics_from_text(text: str) -> List[str]:
    """Extract topics from text using simple keyword matching"""
    import re

    topics = set()
    text_lower = text.lower()

    # Academic topic keywords
    topic_patterns = {
        "machine learning": r"\bmachine learning\b|\bml\b|\bneural network\b|\bdeep learning\b",
        "computer vision": r"\bcomputer vision\b|\bcv\b|\bimage recognition\b|\bobject detection\b",
        "natural language processing": r"\bnatural language processing\b|\bnlp\b|\btext analysis\b",
        "quantum computing": r"\bquantum computer\b|\bquantum computing\b|\bquantum algorithm\b",
        "robotics": r"\brobotic\b|\brobotics\b|\bautonomous\b",
        "algorithms": r"\balgorithm\b|\boptimization\b|\bcomplexity\b",
        "databases": r"\bdatabase\b|\bdata management\b|\bsql\b|\bnosql\b",
        "security": r"\bcryptography\b|\bsecurity\b|\bprivacy\b|\bencryption\b",
        "graph theory": r"\bgraph\b|\bnetwork\b|\bnode\b|\bedge\b",
        "statistics": r"\bstatistic\b|\bprobability\b|\bbayesian\b",
        "bioinformatics": r"\bbioinformatic\b|\bgenomics\b|\bdna\b|\bprotein\b",
        "distributed systems": r"\bdistributed\b|\bscalable\b|\bparallel\b",
    }

    for topic, pattern in topic_patterns.items():
        if re.search(pattern, text_lower):
            topics.add(topic)

    return list(topics)


async def _extract_keyphrases_from_text(text: str) -> List[str]:
    """Extract key phrases from text using simple n-gram analysis"""
    import re
    from collections import Counter

    # Clean and tokenize text
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())

    # Common academic words to filter out
    stop_words = {
        "that",
        "this",
        "with",
        "from",
        "they",
        "have",
        "been",
        "than",
        "were",
        "their",
        "what",
        "when",
        "where",
        "will",
        "about",
        "would",
        "could",
        "should",
        "these",
        "those",
        "through",
        "between",
        "after",
        "before",
        "however",
        "therefore",
        "because",
        "although",
        "though",
        "thus",
        "hence",
        "thereby",
        "therein",
        "thereof",
        "hereto",
        "herewith",
    }

    # Filter stop words
    words = [w for w in words if w not in stop_words]

    # Count word frequencies
    word_freq = Counter(words)

    # Extract common bigrams and trigrams
    words_text = " ".join(words)
    bigrams = []
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i+1]}"
        if words_text.count(bigram) > 1:  # Appears at least twice
            bigrams.append(bigram)

    # Get top words and phrases
    top_words = [w for w, c in word_freq.most_common(10)]
    top_phrases = list(set(bigrams))[:5]

    # Combine and return
    keyphrases = top_words + top_phrases

    return keyphrases[:15]  # Return up to 15 key phrases


def _extract_title_from_text(text: str) -> str:
    """Extract title from the beginning of text content"""
    import re

    # Get first few lines
    lines = text.split("\n")[:10]

    # Look for a title-like line (usually the first significant line)
    for line in lines:
        line = line.strip()
        # Skip empty lines and page numbers
        if not line or re.match(r"^\d+$", line):
            continue

        # Skip lines that are too long (might be paragraphs)
        if len(line) > 200:
            continue

        # Skip lines with common non-title patterns
        skip_patterns = ["abstract", "introduction", "1.", "page", "arxiv:", "http"]
        if any(pattern in line.lower() for pattern in skip_patterns):
            continue

        # This could be a title
        # Clean up common PDF artifacts
        title = re.sub(r"\s+", " ", line)
        title = title.strip()

        # Return if it looks reasonable
        if 5 < len(title) < 150:
            return title

    # Fallback: use first meaningful line
    for line in lines:
        line = line.strip()
        if line and len(line) > 10 and len(line) < 150:
            return re.sub(r"\s+", " ", line)

    return ""


async def _extract_topics_from_filename(filename: str) -> List[str]:
    """Extract topics from filename based on common patterns"""
    topics = []
    filename_lower = filename.lower()

    # Common topic keywords in ArXiv papers
    topic_keywords = {
        "learning": ["Machine Learning", "Deep Learning"],
        "neural": ["Neural Networks", "Deep Learning"],
        "quantum": ["Quantum Computing", "Quantum Physics"],
        "optimization": ["Optimization", "Mathematical Optimization"],
        "algorithm": ["Algorithms", "Computer Science"],
        "network": ["Networks", "Graph Theory"],
        "analysis": ["Data Analysis", "Statistical Analysis"],
        "classification": ["Classification", "Machine Learning"],
        "detection": ["Object Detection", "Pattern Recognition"],
        "attention": ["Attention Mechanism", "Transformers"],
        "transformer": ["Transformers", "NLP"],
        "clustering": ["Clustering", "Unsupervised Learning"],
        "reinforcement": ["Reinforcement Learning", "RL"],
        "cnn": ["CNN", "Computer Vision"],
        "gan": ["GAN", "Generative Models"],
        "graph": ["Graph Neural Networks", "GNN"],
        "nlp": ["NLP", "Natural Language Processing"],
    }

    for keyword, topic_list in topic_keywords.items():
        if keyword in filename_lower:
            topics.extend(topic_list)

    return list(set(topics))[:5]  # Return up to 5 topics


def _extract_keyphrases_from_filename(filename: str) -> List[str]:
    """Extract potential key phrases from filename"""
    # Remove file extension and version
    name = filename.replace(".pdf", "").split("v")[0]

    # Split by common separators
    import re

    parts = re.split(r"[_\-\.]", name)

    # Filter out common non-informative parts
    keyphrases = []
    skip_words = {
        "arxiv",
        "paper",
        "pdf",
        "study",
        "analysis",
        "approach",
        "method",
        "system",
    }

    for part in parts:
        if len(part) > 3 and part.lower() not in skip_words:
            # Clean up the part
            clean_part = re.sub(r"[^a-zA-Z0-9]", "", part)
            if clean_part:
                keyphrases.append(clean_part)

    return keyphrases[:10]  # Return up to 10 key phrases
