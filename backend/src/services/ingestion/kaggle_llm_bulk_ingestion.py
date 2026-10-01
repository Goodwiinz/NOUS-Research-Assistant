"""
Kaggle Bulk Ingestion Service with LLM Extraction

This service ingests ArXiv papers from the Kaggle dataset with full LLM-powered
entity extraction, relationship extraction, and vector embeddings.

Features:
- LLM-based entity extraction (methods, models, metrics, tasks, datasets)
- LLM-based relationship extraction (USES, EVALUATED_ON, ACHIEVED)
- Azure OpenAI embeddings stored in Qdrant
- Resume capability for interrupted ingestions
- Progress tracking with callbacks

COST WARNING: Processing 500k papers with LLM extraction is expensive!
- Entity extraction: ~$0.01-0.03 per paper
- Relationship extraction: ~$0.01-0.03 per paper
- Embeddings: ~$0.0001 per paper
- Total estimated cost: $10,000-30,000 for 500k papers
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict

from src.core.neo4j_auth import get_neo4j_auth

logger = logging.getLogger(__name__)


class LLMIngestionProgress(TypedDict):
    """Progress information for LLM bulk ingestion"""

    current_batch: int
    total_batches: int
    processed: int
    total_papers: int
    ingested: int
    failed: int
    skipped: int
    entities_extracted: int
    relationships_extracted: int
    embeddings_generated: int
    progress_percent: float
    papers_per_second: float
    eta_seconds: float
    llm_cost_estimate: float


class KaggleLLMBulkIngestionService:
    """
    Service for bulk ingestion of ArXiv papers from Kaggle with LLM extraction.

    This service provides:
    1. LLM Entity Extraction - via Azure OpenAI
    2. LLM Relationship Extraction - via Azure OpenAI
    3. Vector Embeddings - via Azure OpenAI + Qdrant storage
    4. Neo4j Knowledge Graph storage
    """

    def __init__(
        self,
        batch_size: int = 100,
        max_papers: int = 1000,
        enable_embeddings: bool = True,
        enable_entity_extraction: bool = True,
        enable_relationship_extraction: bool = True,
    ):
        """
        Initialize the LLM bulk ingestion service.

        Args:
            batch_size: Papers per batch (lower = more control, higher = faster)
            max_papers: Maximum papers to ingest
            enable_embeddings: Generate and store vector embeddings
            enable_entity_extraction: Extract entities via LLM
            enable_relationship_extraction: Extract relationships via LLM
        """
        self.batch_size = batch_size
        self.max_papers = max_papers
        self.enable_embeddings = enable_embeddings
        self.enable_entity_extraction = enable_entity_extraction
        self.enable_relationship_extraction = enable_relationship_extraction

        # State management
        self.state_file = Path("/app/data/llm_ingestion_state.json")
        self.state: Dict[str, Any] = {}

        # Statistics
        self.stats = {
            "entities_extracted": 0,
            "relationships_extracted": 0,
            "embeddings_generated": 0,
            "llm_calls": 0,
            "llm_cost_estimate": 0.0,
        }

        # Lazy-loaded services
        self._neo4j_driver = None
        self._azure_service = None
        self._embedding_service = None

    async def _get_neo4j_driver(self):
        """Lazy load Neo4j driver"""
        if self._neo4j_driver is None:
            from neo4j import AsyncGraphDatabase

            auth = get_neo4j_auth()

            self._neo4j_driver = AsyncGraphDatabase.driver(
                auth.uri,
                auth=(auth.user, auth.password),
                max_connection_lifetime=300,
                max_connection_pool_size=50,
                connection_acquisition_timeout=30,
            )
        return self._neo4j_driver

    async def _get_azure_service(self):
        """Lazy load Azure OpenAI service"""
        if self._azure_service is None:
            from src.services.infrastructure.azure_openai_service import (
                azure_openai_service,
            )

            self._azure_service = azure_openai_service
        return self._azure_service

    async def _get_embedding_service(self):
        """Lazy load embedding service"""
        if self._embedding_service is None:
            from src.services.embedding.embedding_service import embedding_service

            self._embedding_service = embedding_service
        return self._embedding_service

    def _load_state(self) -> Dict[str, Any]:
        """Load ingestion state from file"""
        if self.state_file.exists():
            with open(self.state_file, "r") as f:
                return json.load(f)
        return {}

    def _save_state(self, state: Dict[str, Any]):
        """Save ingestion state to file"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    async def _extract_entities_with_llm(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract entities using Azure OpenAI LLM.

        Extracts:
        - METHODOLOGY: Research methods, algorithms, techniques
        - MODEL: ML/AI models and architectures
        - METRIC: Evaluation metrics
        - TASK: Research tasks
        - DATASET: Datasets used
        - INSTITUTION: Universities, companies
        """
        azure_service = await self._get_azure_service()

        if not azure_service.is_chat_available():
            logger.warning(
                "Azure OpenAI chat not available, skipping entity extraction"
            )
            return []

        prompt = """Extract key technical entities from the following academic paper abstract.

ENTITY TYPES (use ONLY these types, never use "OTHER" or any unlisted type):
- METHODOLOGY: Research methods, algorithms, techniques, approaches (e.g., "transformer attention", "gradient descent", "backpropagation", "ensemble learning")
- MODEL: ML/AI models, architectures, neural networks (e.g., "BERT", "GPT-4", "ResNet-50", "LSTM", "Transformer")
- METRIC: Evaluation metrics, scores, measurements (e.g., "F1 score", "BLEU", "accuracy", "precision", "recall", "AUC")
- TASK: Research tasks, problems being solved (e.g., "text classification", "image segmentation", "object detection", "machine translation")
- DATASET: Datasets, benchmarks, corpora (e.g., "ImageNet", "COCO", "GLUE", "SQuAD", "MNIST")
- FRAMEWORK: Software frameworks, libraries, tools (e.g., "TensorFlow", "PyTorch", "scikit-learn", "Hugging Face")
- THEORY: Theoretical concepts, mathematical foundations (e.g., "information theory", "Bayesian inference", "game theory")
- DOMAIN: Application domains, fields (e.g., "natural language processing", "computer vision", "healthcare AI", "autonomous driving")
- ARCHITECTURE: System architectures, design patterns (e.g., "encoder-decoder", "attention mechanism", "multi-head attention")
- LOSS_FUNCTION: Loss functions, objectives (e.g., "cross-entropy", "MSE", "contrastive loss", "triplet loss")
- OPTIMIZATION: Optimizers, training techniques (e.g., "Adam", "SGD", "learning rate scheduling", "dropout")
- INSTITUTION: Universities, companies, research labs (e.g., "Google", "Stanford", "OpenAI", "DeepMind")

IMPORTANT:
- Only use the types listed above. Do NOT use "OTHER", "CONCEPT", or any type not in this list.
- If an entity doesn't clearly fit, classify it as the CLOSEST matching type.
- Return a JSON array of objects with 'name', 'type', and 'confidence' (0-1).
- Only include entities with confidence > 0.7.

Text:
{text}

Response (JSON array only):"""

        try:
            response = await azure_service.chat_completion(
                messages=[{"role": "user", "content": prompt.format(text=text[:2000])}],
                max_tokens=1000,
                temperature=0.1,
            )

            self.stats["llm_calls"] += 1
            self.stats["llm_cost_estimate"] += 0.02  # Approximate cost per call

            # Parse JSON response
            content = response.get("content", "[]")
            # Try to extract JSON from response
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            entities = json.loads(content.strip())
            self.stats["entities_extracted"] += len(entities)
            return entities

        except Exception as e:
            logger.warning(f"Entity extraction failed: {e}")
            return []

    async def _extract_relationships_with_llm(
        self, text: str, entities: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Extract relationships between entities using Azure OpenAI LLM.

        Relationship types:
        - USES: Model/method uses another method/technique
        - EVALUATED_ON: Model evaluated on dataset
        - ACHIEVED: Model achieved metric result
        - COMPARED_WITH: Model compared with another
        - EXTENDS: Method extends/builds on another
        """
        azure_service = await self._get_azure_service()

        if not azure_service.is_chat_available() or not entities:
            return []

        entity_names = [e.get("name", "") for e in entities[:15]]  # Limit entities

        prompt = """Given these entities extracted from a research paper:
{entities}

And this text:
{text}

Extract relationships between entities. Use these relationship types:
- USES: (source) uses (target) technique/method
- EVALUATED_ON: (model) evaluated on (dataset)
- ACHIEVED: (model/method) achieved (metric/result)
- COMPARED_WITH: (model) compared with (model)
- EXTENDS: (method) extends/builds on (method)

Return a JSON array of objects with 'source', 'target', 'relationship', 'confidence'.
Only include relationships with confidence > 0.6.

Response (JSON array only):"""

        try:
            response = await azure_service.chat_completion(
                messages=[
                    {
                        "role": "user",
                        "content": prompt.format(
                            entities=", ".join(entity_names), text=text[:1500]
                        ),
                    }
                ],
                max_tokens=800,
                temperature=0.1,
            )

            self.stats["llm_calls"] += 1
            self.stats["llm_cost_estimate"] += 0.02

            content = response.get("content", "[]")
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            relationships = json.loads(content.strip())
            self.stats["relationships_extracted"] += len(relationships)
            return relationships

        except Exception as e:
            logger.warning(f"Relationship extraction failed: {e}")
            return []

    async def _generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for texts using Azure OpenAI"""
        embedding_service = await self._get_embedding_service()

        try:
            # Use Azure OpenAI embeddings
            result = await embedding_service.generate_batch_embeddings_azure(texts)

            if result.embeddings:
                self.stats["embeddings_generated"] += len(
                    [e for e in result.embeddings if e]
                )
                return result.embeddings
            return []

        except Exception as e:
            logger.warning(f"Embedding generation failed: {e}")
            return []

    async def _process_paper_with_llm(
        self, session, paper: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process a single paper with full LLM extraction.

        Returns processing result with entities, relationships, and embedding status.
        """
        paper_id = paper.get("id", "")
        title = paper.get("title", "")
        abstract = paper.get("abstract", "")
        categories = paper.get("categories", "")
        authors = paper.get("authors", "")

        # Combine title and abstract for extraction
        text = f"{title}\n\n{abstract}"

        result = {
            "paper_id": paper_id,
            "success": False,
            "entities": [],
            "relationships": [],
            "embedding_stored": False,
        }

        try:
            # 1. Create base document node in Neo4j
            await session.run(
                """
                MERGE (d:DOCUMENT {id: $id})
                SET d.title = $title,
                    d.abstract = $abstract,
                    d.arxiv_category = $category,
                    d.authors = $authors,
                    d.source = 'kaggle_arxiv',
                    d.extraction_type = 'llm',
                    d.processed_at = datetime()
            """,
                {
                    "id": paper_id,
                    "title": title[:500],
                    "abstract": abstract[:2000],
                    "category": categories.split()[0] if categories else "",
                    "authors": authors[:500],
                },
            )

            # 2. Extract entities via LLM
            entities = []
            if self.enable_entity_extraction and abstract:
                entities = await self._extract_entities_with_llm(text)
                result["entities"] = entities

                # Store entities in Neo4j
                for entity in entities:
                    entity_type = entity.get("type", "CONCEPT").upper()
                    entity_name = entity.get("name", "")
                    confidence = entity.get("confidence", 0.8)

                    if entity_name:
                        await session.run(
                            f"""
                            MERGE (e:Entity:{entity_type} {{name: $name}})
                            SET e.confidence = $confidence, e.type = $entity_type
                            WITH e
                            MATCH (d:DOCUMENT {{id: $paper_id}})
                            MERGE (d)-[:MENTIONS {{confidence: $confidence}}]->(e)
                        """,
                            {
                                "name": entity_name,
                                "confidence": confidence,
                                "paper_id": paper_id,
                                "entity_type": entity_type,
                            },
                        )

            # 3. Extract relationships via LLM
            if self.enable_relationship_extraction and entities:
                relationships = await self._extract_relationships_with_llm(
                    text, entities
                )
                result["relationships"] = relationships

                # Store relationships in Neo4j
                for rel in relationships:
                    source = rel.get("source", "")
                    target = rel.get("target", "")
                    rel_type = rel.get("relationship", "RELATED_TO").upper()
                    confidence = rel.get("confidence", 0.7)

                    if source and target:
                        await session.run(
                            f"""
                            MATCH (s:Entity {{name: $source}})
                            MATCH (t:Entity {{name: $target}})
                            MERGE (s)-[r:{rel_type}]->(t)
                            SET r.confidence = $confidence,
                                r.source_paper = $paper_id
                        """,
                            {
                                "source": source,
                                "target": target,
                                "confidence": confidence,
                                "paper_id": paper_id,
                            },
                        )

            # 4. Generate embedding (Qdrant vector store retired; nothing to
            # persist to, but the embedding count is still surfaced in stats).
            if self.enable_embeddings and text:
                await self._generate_embeddings([text])

            result["success"] = True
            return result

        except Exception as e:
            logger.error(f"Failed to process paper {paper_id}: {e}")
            result["error"] = str(e)
            return result

    async def _process_batch_with_llm(
        self, session, papers: List[Dict[str, Any]], batch_num: int
    ) -> Dict[str, int]:
        """Process a batch of papers with LLM extraction"""
        stats = {"ingested": 0, "failed": 0, "skipped": 0}

        for paper in papers:
            paper_id = paper.get("id", "")

            # Check if already processed
            result = await session.run(
                "MATCH (d:DOCUMENT {id: $id}) WHERE d.extraction_type = 'llm' RETURN d",
                {"id": paper_id},
            )
            if await result.single():
                stats["skipped"] += 1
                continue

            # Process with LLM
            proc_result = await self._process_paper_with_llm(session, paper)

            if proc_result["success"]:
                stats["ingested"] += 1
            else:
                stats["failed"] += 1

            # Small delay to avoid rate limiting
            await asyncio.sleep(0.1)

        return stats

    async def run_ingestion(
        self,
        categories: Optional[List[str]] = None,
        resume: bool = True,
        progress_callback: Optional[Callable[[LLMIngestionProgress], None]] = None,
    ) -> Dict[str, Any]:
        """
        Run bulk ingestion with LLM extraction.

        Args:
            categories: Filter to specific ArXiv categories (e.g., ["cs.AI", "cs.LG"])
            resume: Resume from previous ingestion state
            progress_callback: Callback for progress updates

        Returns:
            Ingestion result statistics
        """
        start_time = time.time()

        # Load state if resuming
        if resume:
            self.state = self._load_state()
        else:
            self.state = {}

        start_batch = self.state.get("last_batch", 0)

        logger.info(
            f"Starting LLM bulk ingestion (max={self.max_papers}, batch={self.batch_size})"
        )
        logger.info(
            f"Features: entities={self.enable_entity_extraction}, relationships={self.enable_relationship_extraction}, embeddings={self.enable_embeddings}"
        )

        # Load Kaggle dataset
        import kagglehub

        dataset_path = kagglehub.dataset_download("Cornell-University/arxiv")
        json_file = Path(dataset_path) / "arxiv-metadata-oai-snapshot.json"

        if not json_file.exists():
            raise FileNotFoundError(f"Dataset not found at {json_file}")

        # Connect to Neo4j
        driver = await self._get_neo4j_driver()

        total_stats = {"processed": 0, "ingested": 0, "failed": 0, "skipped": 0}

        papers_buffer = []
        batch_count = 0
        total_batches = (self.max_papers + self.batch_size - 1) // self.batch_size

        try:
            async with driver.session() as session:
                # Ensure indexes exist
                await session.run(
                    "CREATE INDEX IF NOT EXISTS FOR (d:DOCUMENT) ON (d.id)"
                )
                await session.run(
                    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name)"
                )

                with open(json_file, "r") as f:
                    for line in f:
                        if total_stats["processed"] >= self.max_papers:
                            break

                        paper = json.loads(line)

                        # Filter by category if specified
                        if categories:
                            paper_cats = paper.get("categories", "").split()
                            if not any(cat in paper_cats for cat in categories):
                                continue

                        papers_buffer.append(paper)

                        # Process batch
                        if len(papers_buffer) >= self.batch_size:
                            batch_count += 1

                            # Skip already processed batches
                            if batch_count <= start_batch:
                                papers_buffer = []
                                continue

                            batch_stats = await self._process_batch_with_llm(
                                session, papers_buffer, batch_count
                            )

                            total_stats["processed"] += len(papers_buffer)
                            total_stats["ingested"] += batch_stats["ingested"]
                            total_stats["failed"] += batch_stats["failed"]
                            total_stats["skipped"] += batch_stats["skipped"]

                            # Save state
                            self.state["last_batch"] = batch_count
                            self.state["total_processed"] = total_stats["processed"]
                            self._save_state(self.state)

                            # Progress callback
                            if progress_callback:
                                elapsed = time.time() - start_time
                                pps = (
                                    total_stats["processed"] / elapsed
                                    if elapsed > 0
                                    else 0
                                )
                                eta = (
                                    (self.max_papers - total_stats["processed"]) / pps
                                    if pps > 0
                                    else 0
                                )

                                progress = LLMIngestionProgress(
                                    current_batch=batch_count,
                                    total_batches=total_batches,
                                    processed=total_stats["processed"],
                                    total_papers=self.max_papers,
                                    ingested=total_stats["ingested"],
                                    failed=total_stats["failed"],
                                    skipped=total_stats["skipped"],
                                    entities_extracted=self.stats["entities_extracted"],
                                    relationships_extracted=self.stats[
                                        "relationships_extracted"
                                    ],
                                    embeddings_generated=self.stats[
                                        "embeddings_generated"
                                    ],
                                    progress_percent=(
                                        total_stats["processed"] / self.max_papers
                                    )
                                    * 100,
                                    papers_per_second=pps,
                                    eta_seconds=eta,
                                    llm_cost_estimate=self.stats["llm_cost_estimate"],
                                )
                                progress_callback(progress)

                            papers_buffer = []

                # Process remaining papers
                if papers_buffer and total_stats["processed"] < self.max_papers:
                    batch_count += 1
                    batch_stats = await self._process_batch_with_llm(
                        session, papers_buffer, batch_count
                    )
                    total_stats["processed"] += len(papers_buffer)
                    total_stats["ingested"] += batch_stats["ingested"]
                    total_stats["failed"] += batch_stats["failed"]
                    total_stats["skipped"] += batch_stats["skipped"]

        finally:
            if self._neo4j_driver:
                await self._neo4j_driver.close()

        elapsed_time = time.time() - start_time

        return {
            "status": "completed",
            "total_processed": total_stats["processed"],
            "total_ingested": total_stats["ingested"],
            "total_failed": total_stats["failed"],
            "total_skipped": total_stats["skipped"],
            "entities_extracted": self.stats["entities_extracted"],
            "relationships_extracted": self.stats["relationships_extracted"],
            "embeddings_generated": self.stats["embeddings_generated"],
            "llm_calls": self.stats["llm_calls"],
            "estimated_cost_usd": self.stats["llm_cost_estimate"],
            "elapsed_seconds": elapsed_time,
            "papers_per_second": (
                total_stats["processed"] / elapsed_time if elapsed_time > 0 else 0
            ),
        }
