"""
Evidence Agreement Meter API endpoints
"""

import logging
import threading
import time
from typing import Annotated, Dict, List, Optional
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BeforeValidator
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ...core.config import settings
from ...core.database import get_db_sync
from ...core.dependencies import get_current_user
from ...middleware.rate_limiting import get_rate_limiter
from ...models.document import Document, ProcessingStatus
from ...models.evidence import StanceClassificationModel
from ...services.evidence import (
    BatchClassificationLimitError,
    BatchClassificationTimeoutError,
    ConsensusCalculator,
    DuplicateSourceIdsError,
    EvidenceCacheService,
    EvidenceSourceLoader,
    EvidenceSourceSet,
    NoActiveSourcesError,
    SourceNotReadyError,
    SourceSetNotFoundError,
    StanceClassifier,
)
from ...services.evidence.source_loader import current_content_hash
from .schemas import EvidenceBreakdown, EvidenceMeter, Stance, StanceBreakdownItem

logger = logging.getLogger(__name__)


# Rate limiter for evidence endpoints
class EvidenceRateLimiter:
    """Rate limiter for evidence API endpoints"""

    def __init__(self, max_requests: int = 60, window_minutes: int = 1):
        self.max_requests = max_requests
        self.window_minutes = window_minutes
        self._rate_limiter = None
        self._lock = threading.Lock()

    def check_rate_limit(self, identifier: str) -> bool:
        """Check if request is allowed, raises HTTPException if not."""
        rate_limiter = self._get_rate_limiter()
        allowed, info = rate_limiter.is_allowed(
            key=identifier,
            limit=self.max_requests,
            window=self.window_minutes * 60,
        )

        if not allowed:
            retry_after = info.get("retry_after", self.window_minutes * 60)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded. Max {self.max_requests} requests "
                    f"per {self.window_minutes} minute(s)."
                ),
                headers={
                    "X-RateLimit-Limit": str(self.max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(
                        int(
                            info.get(
                                "reset_time", time.time() + (self.window_minutes * 60)
                            )
                        )
                    ),
                    "Retry-After": str(retry_after),
                },
            )

        return True

    def _get_rate_limiter(self):
        """Initialize rate limiter once; prefer Redis for multi-worker safety."""
        if self._rate_limiter is None:
            with self._lock:
                if self._rate_limiter is None:
                    try:
                        redis_client = redis.Redis.from_url(
                            settings.REDIS_URL or "redis://localhost:6379/0",
                            decode_responses=True,
                        )
                        redis_client.ping()
                        self._rate_limiter = get_rate_limiter(redis_client)
                        logger.info("Evidence rate limiter initialized with Redis")
                    except Exception as e:
                        logger.warning(
                            f"Redis unavailable for evidence rate limiting, using in-memory: {e}"
                        )
                        self._rate_limiter = get_rate_limiter(None)
        return self._rate_limiter


evidence_rate_limiter = EvidenceRateLimiter(max_requests=60, window_minutes=1)


async def rate_limit_dependency(request: Request):
    """Dependency to enforce rate limiting"""
    client_ip = request.client.host if request.client else "unknown"
    route_name = request.url.path.rsplit("/", 1)[-1]
    rate_key = f"evidence:ip:{client_ip}:{route_name}"
    evidence_rate_limiter.check_rate_limit(rate_key)
    return True


router = APIRouter()

# Initialize services
cache_service = EvidenceCacheService()
stance_classifier = StanceClassifier(cache_service)
consensus_calculator = ConsensusCalculator()
source_loader = EvidenceSourceLoader()


def _classifier_version() -> str:
    """Read the classifier pipeline identity while tolerating test doubles."""
    classifier_version = getattr(stance_classifier, "classifier_version", None)
    if isinstance(classifier_version, str):
        return classifier_version
    return str(getattr(stance_classifier, "model_version"))


def _batch_classification_limit_detail() -> str:
    """Expose the authored limit reason, never text from the caught exception."""
    limit = stance_classifier.max_batch_sources
    # Use only the configured integer so the public reason follows limit changes
    # without admitting arbitrary configuration or exception text into responses.
    if type(limit) is not int or limit < 1:
        return "Batch classification limit exceeded"
    return f"Maximum {limit} sources allowed per batch classification request"


def _parse_source_id(value: object) -> UUID:
    """Parse classify request IDs while preserving the endpoint's 400 contract."""
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        # FastAPI normally turns UUID validation failures into a 422 before the
        # handler runs. The evidence API intentionally exposes malformed IDs as
        # a caller error (400), matching the meter endpoint and public contract.
        raise HTTPException(status_code=400, detail="Invalid source ID format") from exc


EvidenceSourceId = Annotated[UUID, BeforeValidator(_parse_source_id)]


def _load_sources_or_http_error(
    db: Session,
    *,
    organization_id: UUID | None,
    source_ids: List[UUID],
    claim: str,
) -> EvidenceSourceSet:
    """Translate source-boundary failures into the public API contract."""
    try:
        return source_loader.load(
            db,
            organization_id=organization_id,
            source_ids=source_ids,
            claim=claim,
        )
    except DuplicateSourceIdsError as exc:
        raise HTTPException(
            status_code=400, detail="Duplicate source IDs are not allowed"
        ) from exc
    except SourceSetNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Sources not found or unavailable"
        ) from exc
    except SourceNotReadyError as exc:
        raise HTTPException(
            status_code=409,
            detail="One or more sources are not ready for evidence analysis",
        ) from exc
    except NoActiveSourcesError as exc:
        raise HTTPException(
            status_code=400,
            detail="No active sources available for evidence analysis",
        ) from exc


def _save_stance_classifications(
    db: Session,
    classifications: List[Optional[Dict]],
    claim_hash: str,
    claim_text: str,
    classifier_version: str,
    organization_id,
) -> int:
    """
    Persist stance classifications with upsert semantics to avoid duplicate-key races.

    Uses PostgreSQL ON CONFLICT when available and a safe ORM fallback otherwise.

    ``organization_id`` is the tenant boundary — always taken from the authenticated
    user (never request input) and part of the upsert key, so two orgs analyzing the
    same claim/source/model are independent rows.
    """
    valid_classifications = [
        classification for classification in classifications if classification
    ]
    if not valid_classifications:
        return 0

    def _as_uuid(value: object) -> UUID:
        return value if isinstance(value, UUID) else UUID(str(value))

    def _org_as_uuid(value: object) -> UUID | None:
        # organization_id may legitimately be None for a user without an org; keep it
        # NULL rather than coercing. The /breakdown read explicitly rejects NULL-org
        # users, so NULL-org rows are never returned to anyone (fail-closed).
        if value is None:
            return None
        return value if isinstance(value, UUID) else UUID(str(value))

    org_uuid = _org_as_uuid(organization_id)

    rows = [
        {
            "claim_hash": claim_hash,
            "claim_text": claim_text,
            "source_id": _as_uuid(classification["source_id"]),
            "source_content_hash": classification.get("source_content_hash"),
            "organization_id": org_uuid,
            "stance": classification["stance"],
            "confidence": classification["confidence"],
            "justification_excerpt": classification.get("justification_excerpt"),
            "model_version": classifier_version,
            "inference_model_version": classification["model_version"],
        }
        for classification in valid_classifications
    ]

    dialect_name = db.bind.dialect.name if db.bind is not None else ""

    if dialect_name == "postgresql":
        insert_stmt = pg_insert(StanceClassificationModel).values(rows)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[
                "claim_hash",
                "source_id",
                "model_version",
                "organization_id",
            ],
            set_={
                "stance": insert_stmt.excluded.stance,
                "confidence": insert_stmt.excluded.confidence,
                "justification_excerpt": insert_stmt.excluded.justification_excerpt,
                "claim_text": insert_stmt.excluded.claim_text,
                "source_content_hash": insert_stmt.excluded.source_content_hash,
                "inference_model_version": insert_stmt.excluded.inference_model_version,
                "updated_at": func.now(),
            },
        )
        db.execute(upsert_stmt)
        return len(rows)

    # SQLite/test fallback
    for row in rows:
        existing = (
            db.query(StanceClassificationModel)
            .filter(
                StanceClassificationModel.claim_hash == row["claim_hash"],
                StanceClassificationModel.source_id == row["source_id"],
                StanceClassificationModel.model_version == row["model_version"],
                StanceClassificationModel.organization_id == row["organization_id"],
            )
            .one_or_none()
        )
        if existing:
            existing.claim_text = row["claim_text"]
            existing.source_content_hash = row["source_content_hash"]
            existing.stance = row["stance"]
            existing.confidence = row["confidence"]
            existing.justification_excerpt = row["justification_excerpt"]
            existing.inference_model_version = row["inference_model_version"]
        else:
            db.add(StanceClassificationModel(**row))

    return len(rows)


def _generate_reproducibility_hash(
    claim_hash: str, source_ids: List[str], classifier_version: str
) -> str:
    """Generate reproducibility hash for API response"""
    return consensus_calculator._generate_reproducibility_hash(
        claim_hash, source_ids, classifier_version
    )


@router.get("/meter", response_model=EvidenceMeter)
async def get_evidence_meter(
    claim: str = Query(
        ..., min_length=10, max_length=1000, description="The claim to evaluate"
    ),
    source_ids: Optional[str] = Query(None, description="Comma-separated source IDs"),
    query_id: Optional[str] = Query(None, description="Optional query ID for context"),
    _rate_limit: bool = Depends(rate_limit_dependency),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Get consensus meter for a claim across sources

    Returns aggregated stance statistics and consensus level
    """

    try:
        # Parse source IDs if provided
        parsed_source_ids = []
        if source_ids:
            try:
                parsed_source_ids = [UUID(s.strip()) for s in source_ids.split(",")]
            except ValueError:
                logger.warning("Invalid source ID format", exc_info=True)
                raise HTTPException(status_code=400, detail="Invalid source ID format")

        if not parsed_source_ids:
            raise HTTPException(status_code=400, detail="source_ids parameter required")

        try:
            stance_classifier.validate_batch_size(len(parsed_source_ids))
        except BatchClassificationLimitError as e:
            logger.warning("Batch classification limit exceeded", exc_info=True)
            raise HTTPException(
                status_code=400, detail=_batch_classification_limit_detail()
            ) from e

        loaded = _load_sources_or_http_error(
            db,
            organization_id=current_user.organization_id,
            source_ids=parsed_source_ids,
            claim=claim,
        )
        classifier_sources = [source.classifier_input() for source in loaded.sources]
        source_revisions = loaded.revisions
        retracted_source_ids = list(loaded.withdrawn_source_ids)
        db.rollback()
        claim_hash = consensus_calculator._generate_claim_hash(claim)
        classifier_version = _classifier_version()

        # Check cache first (keyed by org — see cache_service.set_evidence_meter)
        cached_meter = await cache_service.get_evidence_meter(
            claim_hash,
            source_revisions,
            classifier_version,
            current_user.organization_id,
        )

        if cached_meter:
            logger.info(
                "Returning cached evidence meter claim_hash=%s source_count=%d",
                claim_hash,
                len(source_revisions),
            )
            cached_meter["cached"] = True
            return EvidenceMeter(**cached_meter)

        logger.info(
            "Classifying evidence sources claim_hash=%s source_count=%d",
            claim_hash,
            len(classifier_sources),
        )

        # Classify stances in parallel
        try:
            classifications = await stance_classifier.classify_sources_batch(
                claim=claim,
                claim_hash=claim_hash,
                sources=classifier_sources,
            )
        except BatchClassificationLimitError:
            logger.warning("Batch classification limit exceeded", exc_info=True)
            raise HTTPException(
                status_code=400, detail=_batch_classification_limit_detail()
            )
        except BatchClassificationTimeoutError:
            logger.warning("Batch classification timed out", exc_info=True)
            raise HTTPException(
                status_code=504, detail="Batch classification timed out"
            )

        # Calculate consensus
        evidence_meter = consensus_calculator.calculate_consensus(
            claim=claim,
            classifications=classifications,
            retracted_source_ids=retracted_source_ids,
            source_revisions=source_revisions,
            classifier_version=classifier_version,
        )

        # Store in database (upsert to avoid duplicates/races)
        saved_count = _save_stance_classifications(
            db=db,
            classifications=classifications,
            claim_hash=claim_hash,
            claim_text=claim,
            classifier_version=classifier_version,
            organization_id=current_user.organization_id,
        )

        try:
            db.commit()
            logger.info("Saved/upserted stance classifications count=%d", saved_count)
        except Exception as exc:
            db.rollback()
            logger.error("Failed to save stance classifications")
            raise HTTPException(
                status_code=500, detail="Failed to generate evidence meter"
            ) from exc

        # Cache the result — keyed by org so cross-tenant requests never share entries
        meter_dict = evidence_meter.model_dump()
        meter_dict["cached"] = False

        await cache_service.set_evidence_meter(
            claim_hash,
            source_revisions,
            classifier_version,
            current_user.organization_id,
            meter_dict,
            ttl=86400,  # 24 hour cache
        )

        logger.info(
            "Evidence meter generated claim_hash=%s source_count=%d",
            claim_hash,
            len(source_revisions),
        )
        return evidence_meter

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to generate evidence meter")
        raise HTTPException(status_code=500, detail="Failed to generate evidence meter")


@router.get("/breakdown", response_model=EvidenceBreakdown)
async def get_evidence_breakdown(
    claim_hash: str = Query(..., description="SHA256 hash of claim"),
    stance_filter: Optional[Stance] = Query(
        None, description="Filter by specific stance"
    ),
    limit: int = Query(20, ge=1, le=100, description="Maximum sources to return"),
    offset: int = Query(0, ge=0, description="Number of sources to skip"),
    _rate_limit: bool = Depends(rate_limit_dependency),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Get detailed breakdown of source stances for a claim

    Returns individual source classifications with excerpts and confidence scores
    """

    try:
        # Fail closed for users without an org. SQLAlchemy compiles
        # ``Column == None`` to ``IS NULL``, which would otherwise match every NULL-org
        # row (a shared bucket across all such users) — a tenant-isolation gap. Reject
        # explicitly so no classifications are ever returned without a real org scope.
        # organization_id is always taken from the authenticated user, never request input.
        if current_user.organization_id is None:
            raise HTTPException(
                status_code=404, detail="No classifications found for this claim"
            )

        # Join classifications to their current, tenant-owned documents. This keeps
        # deleted, incomplete, and legacy rows out of the provenance response.
        query = (
            db.query(
                StanceClassificationModel.source_id,
                StanceClassificationModel.claim_text,
                StanceClassificationModel.stance,
                StanceClassificationModel.confidence,
                StanceClassificationModel.justification_excerpt,
                StanceClassificationModel.source_content_hash,
                Document.title,
                Document.content_text,
            )
            .join(Document, StanceClassificationModel.source_id == Document.id)
            .filter(
                StanceClassificationModel.claim_hash == claim_hash,
                StanceClassificationModel.model_version == _classifier_version(),
                StanceClassificationModel.organization_id
                == current_user.organization_id,
                StanceClassificationModel.claim_text.isnot(None),
                Document.organization_id == current_user.organization_id,
                Document.is_deleted.is_(False),
                Document.processing_status == ProcessingStatus.COMPLETED,
            )
            .order_by(
                StanceClassificationModel.confidence.desc(),
                StanceClassificationModel.source_id.asc(),
            )
        )

        if stance_filter:
            query = query.filter(
                StanceClassificationModel.stance == stance_filter.value
            )

        current_classifications = []
        visible_current_count = 0
        for candidate in query.yield_per(100):
            (
                source_id,
                claim_text,
                stance,
                confidence,
                justification_excerpt,
                source_content_hash,
                title,
                content_text,
            ) = candidate
            current_hash = current_content_hash(content_text)
            if current_hash is None or source_content_hash != current_hash:
                continue

            if visible_current_count < offset:
                visible_current_count += 1
                continue

            current_classifications.append(
                (
                    source_id,
                    claim_text,
                    stance,
                    confidence,
                    justification_excerpt,
                    title,
                )
            )
            visible_current_count += 1
            if len(current_classifications) >= limit:
                break

        classifications = current_classifications

        if not classifications:
            raise HTTPException(
                status_code=404, detail="No classifications found for this claim"
            )

        sources = []
        for (
            source_id,
            _claim_text,
            stance,
            confidence,
            justification_excerpt,
            title,
        ) in classifications:
            breakdown_item = StanceBreakdownItem(
                source_id=source_id,
                title=title,
                stance=Stance(getattr(stance, "value", stance)),
                confidence=confidence,
                justification_excerpt=justification_excerpt,
                is_retracted=False,
            )
            sources.append(breakdown_item)

        claim_text = classifications[0][1]

        breakdown = EvidenceBreakdown(
            claim=claim_text, claim_hash=claim_hash, sources=sources
        )

        logger.info(
            f"Evidence breakdown returned {len(sources)} sources for claim {claim_hash[:8]}..."
        )
        return breakdown

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to get evidence breakdown")
        raise HTTPException(status_code=500, detail="Failed to get evidence breakdown")


@router.post("/classify", status_code=201)
async def classify_sources_for_claim(
    claim: str,
    source_ids: List[EvidenceSourceId],
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Internal endpoint to classify sources for a claim

    This is used by other services to trigger stance classification
    """

    try:
        try:
            stance_classifier.validate_batch_size(len(source_ids))
        except BatchClassificationLimitError as e:
            logger.warning("Batch classification limit exceeded", exc_info=True)
            raise HTTPException(
                status_code=400, detail=_batch_classification_limit_detail()
            ) from e

        loaded = _load_sources_or_http_error(
            db,
            organization_id=current_user.organization_id,
            source_ids=source_ids,
            claim=claim,
        )
        classifier_sources = [source.classifier_input() for source in loaded.sources]
        db.rollback()
        claim_hash = consensus_calculator._generate_claim_hash(claim)
        classifier_version = _classifier_version()

        # Classify stances
        try:
            classifications = await stance_classifier.classify_sources_batch(
                claim=claim,
                claim_hash=claim_hash,
                sources=classifier_sources,
            )
        except BatchClassificationLimitError:
            logger.warning("Batch classification limit exceeded", exc_info=True)
            raise HTTPException(
                status_code=400, detail=_batch_classification_limit_detail()
            )
        except BatchClassificationTimeoutError:
            logger.warning("Batch classification timed out", exc_info=True)
            raise HTTPException(
                status_code=504, detail="Batch classification timed out"
            )

        # Store results in database (upsert to avoid duplicates/races)
        saved_count = _save_stance_classifications(
            db=db,
            classifications=classifications,
            claim_hash=claim_hash,
            claim_text=claim,
            classifier_version=classifier_version,
            organization_id=current_user.organization_id,
        )

        db.commit()

        return {
            "status": "completed",
            "claim_hash": claim_hash,
            "classifications_created": saved_count,
            "total_sources": len(source_ids),
            "model_version": classifier_version,
        }

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.error("Failed to classify sources")
        raise HTTPException(status_code=500, detail="Classification failed")


# Health check endpoint
@router.get("/health")
async def health_check(_current_user=Depends(get_current_user)):
    """Health check for evidence meter service"""

    try:
        # Check cache service
        cache_connected = cache_service._ensure_connected()

        return {
            "status": "healthy",
            "cache_connected": cache_connected,
            "model_version": _classifier_version(),
            "components": {
                "stance_classifier": "operational",
                "consensus_calculator": "operational",
                "cache_service": "operational" if cache_connected else "degraded",
            },
        }

    except Exception:
        logger.error("Health check failed", exc_info=True)
        return {"status": "unhealthy", "error": "Health check failed"}
