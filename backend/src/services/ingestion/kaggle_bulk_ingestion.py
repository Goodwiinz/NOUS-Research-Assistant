"""
Kaggle ArXiv Bulk Ingestion Service

Ingests 500k+ papers from the Kaggle ArXiv dataset into Neo4j knowledge graph.
Uses streaming to handle large datasets efficiently.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from src.core.neo4j_auth import get_neo4j_auth

logger = logging.getLogger(__name__)


def _import_kagglehub():
    """Lazy import — kagglehub pulls heavy deps and version-mismatches break app import."""
    import kagglehub

    return kagglehub


@dataclass
class IngestionProgress:
    """Tracks ingestion progress"""

    total_papers: int = 0
    processed: int = 0
    ingested: int = 0
    failed: int = 0
    skipped: int = 0
    start_time: Optional[datetime] = None
    current_batch: int = 0
    total_batches: int = 0

    @property
    def elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    @property
    def papers_per_second(self) -> float:
        if self.elapsed_seconds == 0:
            return 0
        return self.processed / self.elapsed_seconds

    @property
    def eta_seconds(self) -> float:
        if self.papers_per_second == 0:
            return 0
        remaining = self.total_papers - self.processed
        return remaining / self.papers_per_second

    @property
    def progress_percent(self) -> float:
        return round(self.processed / max(self.total_papers, 1) * 100, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_papers": self.total_papers,
            "processed": self.processed,
            "ingested": self.ingested,
            "failed": self.failed,
            "skipped": self.skipped,
            "elapsed_seconds": self.elapsed_seconds,
            "papers_per_second": round(self.papers_per_second, 2),
            "eta_seconds": round(self.eta_seconds, 2),
            "current_batch": self.current_batch,
            "total_batches": self.total_batches,
            "progress_percent": self.progress_percent,
        }


class KaggleBulkIngestionService:
    """
    Bulk ingestion service for Kaggle ArXiv dataset.

    Supports:
    - Streaming large datasets
    - Batch processing with configurable size
    - Progress tracking and resumption
    - Category filtering
    - Neo4j knowledge graph integration
    """

    DATASET_NAME = "Cornell-University/arxiv"
    STATE_FILE = Path("data/kaggle_ingestion_state.json")

    def __init__(
        self,
        neo4j_uri: str = None,
        neo4j_user: str = None,
        neo4j_password: str = None,
        batch_size: int = 1000,
        max_papers: int = 500000,
    ):
        if neo4j_uri and neo4j_user and neo4j_password:
            self.neo4j_uri = neo4j_uri
            self.neo4j_user = neo4j_user
            self.neo4j_password = neo4j_password
        else:
            auth = get_neo4j_auth()
            self.neo4j_uri = neo4j_uri or auth.uri
            self.neo4j_user = neo4j_user or auth.user
            self.neo4j_password = neo4j_password or auth.password
        self.batch_size = batch_size
        self.max_papers = max_papers
        self.progress = IngestionProgress()
        self.driver = None
        self._state: Dict[str, Any] = {}
        self._load_state()

    def _load_state(self):
        """Load previous ingestion state for resumption"""
        self.STATE_FILE.parent.mkdir(exist_ok=True)
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, "r") as f:
                    self._state = json.load(f)
                logger.info(
                    f"Loaded state: {self._state.get('processed', 0)} papers previously processed"
                )
            except Exception as e:
                logger.warning(f"Could not load state: {e}")
                self._state = {}

    def _save_state(self):
        """Save current ingestion state"""
        try:
            self._state["processed"] = self.progress.processed
            self._state["ingested"] = self.progress.ingested
            self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(self.STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save state: {e}")

    async def connect_neo4j(self):
        """Connect to Neo4j database"""
        from neo4j import AsyncGraphDatabase

        self.driver = AsyncGraphDatabase.driver(
            self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password)
        )
        # Verify connection
        async with self.driver.session() as session:
            result = await session.run("RETURN 1 as n")
            await result.single()
        logger.info("Connected to Neo4j")

    async def close(self):
        """Close Neo4j connection"""
        if self.driver:
            await self.driver.close()

    async def load_kaggle_dataset_async(self, streaming: bool = True):
        """
        Load the Kaggle ArXiv dataset asynchronously

        Args:
            streaming: Use streaming mode for memory efficiency

        Returns:
            Dataset iterator (generator for JSON lines)
        """
        logger.info(f"Loading ArXiv dataset with streaming={streaming}")

        # Check for cached dataset file first
        cache_dir = (
            Path.home()
            / ".cache"
            / "kagglehub"
            / "datasets"
            / "Cornell-University"
            / "arxiv"
        )
        local_json = None

        # Search for existing downloaded file
        if cache_dir.exists():
            for version_dir in cache_dir.iterdir():
                if version_dir.is_dir():
                    json_file = version_dir / "arxiv-metadata-oai-snapshot.json"
                    if json_file.exists():
                        local_json = json_file
                        logger.info(f"Found cached dataset at {local_json}")
                        break

        # If not found, download in background thread
        if not local_json:
            logger.info(
                "Dataset not cached, downloading via kagglehub (this may take a while)..."
            )

            # Run blocking download in thread pool
            def download_dataset():
                return _import_kagglehub().dataset_download(
                    self.DATASET_NAME,
                )

            try:
                dataset_path = await asyncio.to_thread(download_dataset)
                local_json = Path(dataset_path) / "arxiv-metadata-oai-snapshot.json"
                logger.info(f"Dataset downloaded to {local_json}")
            except Exception as e:
                logger.error(f"Failed to download dataset: {e}")
                raise RuntimeError(f"Could not download ArXiv dataset: {e}")

        if not local_json.exists():
            raise RuntimeError(f"Dataset file not found at {local_json}")

        # Return a generator that reads JSON lines
        return self._json_lines_generator(local_json)

    def _json_lines_generator(self, json_file: Path):
        """
        Generator that yields parsed JSON records from a JSON lines file

        Args:
            json_file: Path to the JSON lines file

        Yields:
            Dict records from the file
        """
        logger.info(f"Reading dataset from {json_file}")
        with open(json_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    yield record
                except json.JSONDecodeError as e:
                    logger.debug(f"Failed to parse line: {e}")
                    continue

    def load_kaggle_dataset(self, streaming: bool = True):
        """
        Synchronous wrapper for backward compatibility - NOT RECOMMENDED
        Use load_kaggle_dataset_async instead
        """
        logger.warning("Using synchronous load - this may block the event loop!")

        # Check for cached file
        cache_dir = (
            Path.home()
            / ".cache"
            / "kagglehub"
            / "datasets"
            / "Cornell-University"
            / "arxiv"
        )
        if cache_dir.exists():
            for version_dir in cache_dir.iterdir():
                if version_dir.is_dir():
                    json_file = version_dir / "arxiv-metadata-oai-snapshot.json"
                    if json_file.exists():
                        logger.info(f"Using cached dataset at {json_file}")
                        return self._json_lines_generator(json_file)

        # Fall back to download (blocking)
        dataset_path = _import_kagglehub().dataset_download(self.DATASET_NAME)
        json_file = Path(dataset_path) / "arxiv-metadata-oai-snapshot.json"
        return self._json_lines_generator(json_file)

    def parse_paper(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse a paper record from the Kaggle dataset

        Args:
            record: Raw paper record

        Returns:
            Parsed paper dict or None if invalid
        """
        try:
            # Extract paper ID from the record
            paper_id = record.get("id", "")
            if not paper_id:
                return None

            # Parse authors (stored as JSON string in the dataset)
            authors_raw = record.get("authors_parsed", "[]")
            if isinstance(authors_raw, str):
                try:
                    authors_list = json.loads(authors_raw)
                    authors = [
                        " ".join(filter(None, [a[1], a[0]])).strip()
                        for a in authors_list
                        if isinstance(a, list) and len(a) >= 2
                    ]
                except json.JSONDecodeError:
                    authors = []
            else:
                authors = []

            # Parse categories
            categories_raw = record.get("categories", "")
            categories = categories_raw.split() if categories_raw else []

            # Parse versions for dates
            versions_raw = record.get("versions", "[]")
            published_date = None
            if isinstance(versions_raw, str):
                try:
                    versions = json.loads(versions_raw)
                    if versions and isinstance(versions, list):
                        first_version = versions[0]
                        if isinstance(first_version, dict):
                            published_date = first_version.get("created")
                except json.JSONDecodeError:
                    pass

            return {
                "id": paper_id,
                "title": record.get("title", "").replace("\n", " ").strip(),
                "abstract": record.get("abstract", "").replace("\n", " ").strip(),
                "authors": authors,
                "categories": categories,
                "primary_category": categories[0] if categories else "unknown",
                "published": published_date or record.get("update_date", ""),
                "doi": record.get("doi", ""),
                "journal_ref": record.get("journal-ref", ""),
                "license": record.get("license", ""),
                "source": "kaggle_arxiv",
            }

        except Exception as e:
            logger.debug(f"Failed to parse paper: {e}")
            return None

    async def ingest_paper_to_neo4j(self, session, paper: Dict[str, Any]) -> bool:
        """
        Ingest a single paper into Neo4j

        Args:
            session: Neo4j session
            paper: Parsed paper dict

        Returns:
            True if successful
        """
        try:
            # Create DOCUMENT node
            query = """
            MERGE (d:DOCUMENT {paper_id: $paper_id})
            ON CREATE SET
                d.id = $paper_id,
                d.title = $title,
                d.abstract = $abstract,
                d.arxiv_category = $primary_category,
                d.categories = $categories,
                d.publication_date = $published,
                d.doi = $doi,
                d.journal_ref = $journal_ref,
                d.license = $license,
                d.source = $source,
                d.created_at = datetime(),
                d.entity_count = 0
            ON MATCH SET
                d.title = $title,
                d.abstract = $abstract,
                d.updated_at = datetime()
            RETURN d.paper_id as id
            """

            await session.run(
                query,
                paper_id=paper["id"],
                title=(
                    paper["title"][:500] if paper["title"] else ""
                ),  # Limit title length
                abstract=(
                    paper["abstract"][:5000] if paper["abstract"] else ""
                ),  # Limit abstract
                primary_category=paper["primary_category"],
                categories=paper["categories"],
                published=paper["published"],
                doi=paper.get("doi", ""),
                journal_ref=paper.get("journal_ref", ""),
                license=paper.get("license", ""),
                source=paper["source"],
            )

            # Create author entities and relationships
            for i, author in enumerate(paper["authors"][:20]):  # Limit to 20 authors
                if not author.strip():
                    continue

                author_query = """
                MERGE (a:Entity:PERSON {name: $name})
                ON CREATE SET
                    a.id = randomUUID(),
                    a.type = 'PERSON',
                    a.extraction_method = 'kaggle_bulk',
                    a.confidence_score = 0.95,
                    a.created_at = datetime()
                WITH a
                MATCH (d:DOCUMENT {paper_id: $paper_id})
                MERGE (a)-[r:AUTHORED]->(d)
                ON CREATE SET r.position = $position, r.created_at = datetime()
                """

                await session.run(
                    author_query,
                    name=author[:200],  # Limit name length
                    paper_id=paper["id"],
                    position=i + 1,
                )

            # Create category nodes
            for category in paper["categories"][:10]:  # Limit categories
                cat_query = """
                MERGE (c:CATEGORY {name: $name})
                ON CREATE SET c.id = randomUUID(), c.created_at = datetime()
                WITH c
                MATCH (d:DOCUMENT {paper_id: $paper_id})
                MERGE (d)-[r:IN_CATEGORY]->(c)
                ON CREATE SET r.created_at = datetime()
                """
                await session.run(cat_query, name=category, paper_id=paper["id"])

            return True

        except Exception as e:
            logger.debug(f"Failed to ingest paper {paper.get('id', 'unknown')}: {e}")
            return False

    async def ingest_batch(self, papers: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Ingest a batch of papers

        Args:
            papers: List of parsed papers

        Returns:
            Batch statistics
        """
        stats = {"success": 0, "failed": 0}

        async with self.driver.session() as session:
            for paper in papers:
                try:
                    if await self.ingest_paper_to_neo4j(session, paper):
                        stats["success"] += 1
                    else:
                        stats["failed"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    logger.debug(f"Batch item failed: {e}")

        return stats

    async def run_ingestion(
        self,
        categories: Optional[List[str]] = None,
        resume: bool = True,
        progress_callback: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """
        Run the bulk ingestion process

        Args:
            categories: Filter to specific categories (e.g., ['cs.AI', 'cs.LG'])
            resume: Resume from previous state
            progress_callback: Callback for progress updates

        Returns:
            Ingestion summary
        """
        logger.info(f"Starting bulk ingestion (max: {self.max_papers} papers)")

        # Connect to Neo4j
        await self.connect_neo4j()

        try:
            # Create indexes for performance
            await self._create_indexes()

            # Load dataset asynchronously
            dataset = await self.load_kaggle_dataset_async()

            # Initialize progress
            self.progress.start_time = datetime.now(timezone.utc)
            self.progress.total_papers = self.max_papers

            # Determine starting point for resume
            start_offset = 0
            if resume and self._state.get("processed", 0) > 0:
                start_offset = self._state["processed"]
                self.progress.processed = start_offset
                self.progress.ingested = self._state.get("ingested", 0)
                logger.info(f"Resuming from paper {start_offset}")

            batch = []
            papers_seen = 0

            # Process dataset
            for record in dataset:
                papers_seen += 1

                # Skip already processed
                if papers_seen <= start_offset:
                    continue

                # Check max limit
                if self.progress.processed >= self.max_papers:
                    logger.info(f"Reached max papers limit: {self.max_papers}")
                    break

                # Parse paper
                paper = self.parse_paper(record)
                if not paper:
                    self.progress.skipped += 1
                    self.progress.processed += 1
                    continue

                # Filter by category if specified
                if categories:
                    if not any(cat in paper["categories"] for cat in categories):
                        self.progress.skipped += 1
                        self.progress.processed += 1
                        continue

                batch.append(paper)

                # Process batch
                if len(batch) >= self.batch_size:
                    self.progress.current_batch += 1
                    stats = await self.ingest_batch(batch)

                    self.progress.processed += len(batch)
                    self.progress.ingested += stats["success"]
                    self.progress.failed += stats["failed"]

                    # Save state periodically
                    self._save_state()

                    # Log progress
                    if self.progress.current_batch % 10 == 0:
                        logger.info(
                            f"Progress: {self.progress.processed:,}/{self.max_papers:,} "
                            f"({self.progress.progress_percent:.1f}%) - "
                            f"{self.progress.papers_per_second:.0f} papers/sec - "
                            f"ETA: {self.progress.eta_seconds/60:.1f} min"
                        )

                    # Callback
                    if progress_callback:
                        progress_callback(self.progress.to_dict())

                    batch = []

            # Process remaining batch
            if batch:
                stats = await self.ingest_batch(batch)
                self.progress.processed += len(batch)
                self.progress.ingested += stats["success"]
                self.progress.failed += stats["failed"]

            # Final save
            self._save_state()

            # Summary
            summary = {
                "status": "completed",
                "total_processed": self.progress.processed,
                "total_ingested": self.progress.ingested,
                "total_failed": self.progress.failed,
                "total_skipped": self.progress.skipped,
                "elapsed_seconds": self.progress.elapsed_seconds,
                "papers_per_second": self.progress.papers_per_second,
                "categories_filter": categories,
            }

            logger.info(f"Ingestion completed: {summary}")
            return summary

        finally:
            await self.close()

    async def _create_indexes(self):
        """Create Neo4j indexes for performance"""
        indexes = [
            "CREATE INDEX document_paper_id IF NOT EXISTS FOR (d:DOCUMENT) ON (d.paper_id)",
            "CREATE INDEX document_category IF NOT EXISTS FOR (d:DOCUMENT) ON (d.arxiv_category)",
            "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
            "CREATE INDEX category_name IF NOT EXISTS FOR (c:CATEGORY) ON (c.name)",
        ]

        async with self.driver.session() as session:
            for index in indexes:
                try:
                    await session.run(index)
                except Exception as e:
                    logger.debug(f"Index creation note: {e}")

        logger.info("Neo4j indexes created/verified")


async def run_bulk_ingestion(
    max_papers: int = 500000,
    batch_size: int = 1000,
    categories: Optional[List[str]] = None,
    resume: bool = True,
) -> Dict[str, Any]:
    """
    Convenience function to run bulk ingestion

    Args:
        max_papers: Maximum papers to ingest
        batch_size: Batch size for processing
        categories: Filter by categories (e.g., ['cs.AI', 'cs.LG'])
        resume: Resume from previous state

    Returns:
        Ingestion summary
    """
    service = KaggleBulkIngestionService(batch_size=batch_size, max_papers=max_papers)

    return await service.run_ingestion(categories=categories, resume=resume)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bulk ingest ArXiv papers from Kaggle")
    parser.add_argument(
        "--max-papers", type=int, default=500000, help="Maximum papers to ingest"
    )
    parser.add_argument("--batch-size", type=int, default=1000, help="Batch size")
    parser.add_argument("--categories", nargs="+", help="Filter by categories")
    parser.add_argument(
        "--no-resume", action="store_true", help="Start fresh (don't resume)"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(
        run_bulk_ingestion(
            max_papers=args.max_papers,
            batch_size=args.batch_size,
            categories=args.categories,
            resume=not args.no_resume,
        )
    )
