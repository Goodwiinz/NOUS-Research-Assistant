"""
Integration tests for Evidence Agreement Meter API endpoints
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.evidence.router import (
    _save_stance_classifications,
    classify_sources_for_claim,
    get_evidence_breakdown,
    get_evidence_meter,
    stance_classifier,
)
from src.core.database import get_db_sync
from src.core.dependencies import get_current_user
from src.main import app
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.evidence import StanceClassificationModel
from tests.conftest import _patch_multi_tenancy_middleware, _test_auth_headers

# Test database setup
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_evidence.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override database dependency for testing"""
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


# Stable org identities so tests can seed rows and assert cross-tenant isolation.
# (A fresh uuid4() per request would make the scoped /breakdown filter never match.)
TEST_ORG_ID = uuid4()
TEST_ORG_ID_B = uuid4()
TEST_USER_ID = uuid4()
TEST_USER_ID_B = uuid4()


# Mock user for testing
class MockUser:
    """Mock user object for testing"""

    def __init__(self, organization_id=TEST_ORG_ID, user_id=TEST_USER_ID):
        self.id = user_id
        self.organization_id = organization_id
        self.is_active = True
        self.email = "test@example.com"

    def has_permission(self, role):
        return True

    def can_view_analytics(self):
        return True


# The authenticated user, fixed for the default test org. Tests that need the
# second tenant swap this override (see test_breakdown_isolated_by_organization).
_active_user = MockUser()


def override_get_current_user():
    """Override authentication for testing"""
    return _active_user


def set_active_user(user: MockUser) -> None:
    """Swap the authenticated user mid-test (for cross-tenant assertions)."""
    global _active_user
    _active_user = user


app.dependency_overrides[get_db_sync] = override_get_db
app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture(scope="module")
def test_client():
    """Test client with database override"""
    # Only create the specific tables we need for testing (avoid PostgreSQL-specific types)
    Document.__table__.create(bind=engine, checkfirst=True)
    StanceClassificationModel.__table__.create(bind=engine, checkfirst=True)
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_multi_tenancy_middleware(monkeypatch)
        with TestClient(app, headers=_test_auth_headers()) as client:
            yield client
    StanceClassificationModel.__table__.drop(bind=engine, checkfirst=True)
    Document.__table__.drop(bind=engine, checkfirst=True)


@pytest.fixture
def mock_auth():
    """Mock authentication dependency (already set at app level)"""
    yield


@pytest.fixture
def sample_source_ids():
    """Sample source UUIDs for testing"""
    return [str(uuid4()) for _ in range(3)]


def seed_document(
    db,
    *,
    organization_id=TEST_ORG_ID,
    document_id=None,
    title="Seeded evidence document",
    content_text="This seeded document contains evidence for the test claim.",
    checksum_sha256=None,
    processing_status=ProcessingStatus.COMPLETED,
    is_deleted=False,
):
    """Insert one caller-owned document with explicit evidence source metadata."""
    checksum = (
        checksum_sha256 or hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    )
    document = Document(
        id=document_id or uuid4(),
        title=title,
        filename="evidence.txt",
        file_path="/tmp/evidence.txt",
        file_size_bytes=len(content_text.encode("utf-8")),
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        checksum_sha256=checksum,
        content_text=content_text,
        processing_status=processing_status,
        organization_id=organization_id,
        uploaded_by_user_id=TEST_USER_ID,
        is_deleted=is_deleted,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def response_message(response):
    """Read either the FastAPI detail or this app's normalized error envelope."""
    data = response.json()
    return data.get("detail") or data.get("error", {}).get("message", "")


class TestEvidenceMeterEndpoint:
    """Test /api/v1/evidence/meter endpoint"""

    @patch("src.api.evidence.router.source_loader.load")
    def test_meter_batch_limit_rejected_before_load(
        self, mock_load, test_client, mock_auth
    ):
        """Oversized meter requests fail before touching the source loader."""
        source_ids = [str(uuid4()) for _ in range(101)]
        assert len(set(source_ids)) == 101

        response = test_client.get(
            "/api/v1/evidence/meter",
            params={
                "claim": "A claim with enough length",
                "source_ids": ",".join(source_ids),
            },
        )

        assert response.status_code == 400
        assert (
            response_message(response)
            == "Maximum 100 sources allowed per batch classification request"
        )
        mock_load.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.api.evidence.router.cache_service")
    async def test_meter_releases_loader_transaction_before_cache_await(
        self, mock_cache
    ):
        """The loader read transaction is closed before the cache boundary."""
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Transaction boundary source",
            content_text="Transaction boundary source contains the cache claim.",
        )
        observed_in_transaction = []
        cached_data = {
            "claim": "Transaction boundary claim",
            "claim_hash": "transaction_boundary_hash",
            "total_sources": 1,
            "supporting": 1,
            "opposing": 0,
            "neutral": 0,
            "not_addressed": 0,
            "consensus_level": "insufficient_data",
            "average_confidence": 0.9,
            "retracted_sources": 0,
            "cached": True,
            "reproducibility_hash": "transaction_boundary_revision",
        }

        async def observe_cache(*args, **kwargs):
            observed_in_transaction.append(db.in_transaction())
            return cached_data

        mock_cache.get_evidence_meter = AsyncMock(side_effect=observe_cache)

        try:
            result = await get_evidence_meter(
                claim="Transaction boundary claim",
                source_ids=str(document.id),
                query_id=None,
                _rate_limit=True,
                current_user=MockUser(),
                db=db,
            )

            assert result.cached is True
            assert observed_in_transaction == [False]
            assert db.in_transaction() is False
        finally:
            db.close()

    @pytest.mark.asyncio
    @patch("src.api.evidence.router.cache_service")
    async def test_meter_cache_revision_changes_when_content_changes_without_checksum_update(
        self, mock_cache, test_client
    ):
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Cache revision source",
            content_text="Original extracted content for cache identity.",
        )
        uploaded_checksum = document.checksum_sha256
        observed_revisions = []
        cached_data = {
            "claim": "Cache revision claim",
            "claim_hash": "cache_revision_hash",
            "total_sources": 1,
            "supporting": 1,
            "opposing": 0,
            "neutral": 0,
            "not_addressed": 0,
            "consensus_level": "insufficient_data",
            "average_confidence": 0.9,
            "retracted_sources": 0,
            "cached": True,
            "reproducibility_hash": "cache_revision_reproducibility",
        }

        async def observe_cache(*args, **kwargs):
            observed_revisions.append(args[1])
            return cached_data

        mock_cache.get_evidence_meter = AsyncMock(side_effect=observe_cache)

        try:
            first = await get_evidence_meter(
                claim="Cache revision claim",
                source_ids=str(document.id),
                query_id=None,
                _rate_limit=True,
                current_user=MockUser(),
                db=db,
            )

            document.content_text = "Revised extracted content for cache identity."
            db.commit()

            second = await get_evidence_meter(
                claim="Cache revision claim",
                source_ids=str(document.id),
                query_id=None,
                _rate_limit=True,
                current_user=MockUser(),
                db=db,
            )

            assert first.cached is True
            assert second.cached is True
            assert len(observed_revisions) == 2
            assert observed_revisions[0] != observed_revisions[1]
            assert document.checksum_sha256 == uploaded_checksum
            assert observed_revisions[0] == [
                f"{document.id}:"
                f"{hashlib.sha256('Original extracted content for cache identity.'.encode('utf-8')).hexdigest()}"
            ]
            assert observed_revisions[1] == [
                f"{document.id}:"
                f"{hashlib.sha256('Revised extracted content for cache identity.'.encode('utf-8')).hexdigest()}"
            ]
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    @patch("src.api.evidence.router.consensus_calculator")
    @patch("src.api.evidence.router.cache_service")
    def test_get_evidence_meter_real_source_success(
        self,
        mock_cache,
        mock_consensus,
        mock_classifier,
        test_client,
        mock_auth,
    ):
        """Test successful evidence meter generation"""
        set_active_user(MockUser())
        db = TestingSessionLocal()
        seeded_documents = [
            seed_document(
                db,
                title="First real source",
                content_text="First real source text supports the test claim.",
            ),
            seed_document(
                db,
                title="Second real source",
                content_text="Second real source text gives additional evidence.",
            ),
            seed_document(
                db,
                title="Third real source",
                content_text="Third real source text opposes the test claim.",
            ),
        ]
        source_ids = [str(document.id) for document in seeded_documents]

        # Mock cache miss (async method)
        mock_cache.get_evidence_meter = AsyncMock(return_value=None)
        mock_cache.set_evidence_meter = AsyncMock(return_value=None)

        # Mock stance classifications
        mock_classifications = [
            {
                "source_id": source_ids[0],
                "stance": "supporting",
                "confidence": 0.90,
                "justification_excerpt": "Strong supporting evidence",
                "model_version": "gpt-4o-mini-2024-07-18",
                "source_content_hash": seeded_documents[0].checksum_sha256,
            },
            {
                "source_id": source_ids[1],
                "stance": "supporting",
                "confidence": 0.85,
                "justification_excerpt": "Additional support",
                "model_version": "gpt-4o-mini-2024-07-18",
                "source_content_hash": seeded_documents[1].checksum_sha256,
            },
            {
                "source_id": source_ids[2],
                "stance": "opposing",
                "confidence": 0.80,
                "justification_excerpt": "Contradictory findings",
                "model_version": "gpt-4o-mini-2024-07-18",
                "source_content_hash": seeded_documents[2].checksum_sha256,
            },
        ]
        mock_classifier.classify_sources_batch = AsyncMock(
            return_value=mock_classifications
        )
        mock_classifier.model_version = "gpt-4o-mini-2024-07-18"

        # Mock consensus calculation
        from src.api.evidence.schemas import ConsensusLevel, EvidenceMeter

        mock_meter = EvidenceMeter(
            claim="Test claim",
            claim_hash="abc123",
            total_sources=3,
            supporting=2,
            opposing=1,
            neutral=0,
            not_addressed=0,
            consensus_level=ConsensusLevel.MODERATE_AGREEMENT,
            average_confidence=0.85,
            retracted_sources=0,
            cached=False,
            reproducibility_hash="test_hash",
        )
        mock_consensus.calculate_consensus.return_value = mock_meter
        mock_consensus._generate_claim_hash.return_value = "abc123"

        try:
            # Make request
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={"claim": "Test claim", "source_ids": ",".join(source_ids)},
            )

            assert response.status_code == 200
            data = response.json()

            assert data["claim"] == "Test claim"
            assert data["total_sources"] == 3
            assert data["supporting"] == 2
            assert data["opposing"] == 1
            assert data["consensus_level"] == "moderate_agreement"
            assert data["average_confidence"] == 0.85
            assert data["publication_retraction_check"] == "not_performed"

            payloads = mock_classifier.classify_sources_batch.await_args.kwargs[
                "sources"
            ]
            assert payloads[0]["excerpt"] in seeded_documents[0].content_text
            assert "Mock excerpt" not in payloads[0]["excerpt"]
            assert payloads[0]["content_hash"] == seeded_documents[0].checksum_sha256
        finally:
            db.close()

    def test_get_evidence_meter_missing_claim(self, test_client, mock_auth):
        """Test meter endpoint with missing claim parameter"""
        response = test_client.get("/api/v1/evidence/meter")

        assert response.status_code == 422  # Validation error

    def test_get_evidence_meter_invalid_source_ids(self, test_client, mock_auth):
        """Test meter endpoint with invalid source IDs"""
        response = test_client.get(
            "/api/v1/evidence/meter",
            params={
                "claim": "Test claim",
                "source_ids": "invalid-uuid,another-invalid",
            },
        )

        assert response.status_code == 400
        data = response.json()
        # Check both possible error response formats
        detail = data.get("detail") or data.get("error", {}).get("message", "")
        assert "Invalid source ID format" in detail

    def test_get_evidence_meter_no_sources(self, test_client, mock_auth):
        """Test meter endpoint with no source IDs (MVP requirement)"""
        response = test_client.get(
            "/api/v1/evidence/meter", params={"claim": "Test claim"}
        )

        assert response.status_code == 400
        data = response.json()
        detail = data.get("detail") or data.get("error", {}).get("message", "")
        assert detail == "source_ids parameter required"
        assert "integration with search API pending" not in detail

    @patch("src.api.evidence.router.cache_service")
    def test_get_evidence_meter_cached_result(
        self, mock_cache, test_client, mock_auth, sample_source_ids
    ):
        """Test meter endpoint returning cached result"""
        db = TestingSessionLocal()
        for source_id in sample_source_ids:
            seed_document(db, document_id=source_id)

        # Mock cache hit
        cached_data = {
            "claim": "Test claim",
            "claim_hash": "abc123",
            "total_sources": 3,
            "supporting": 2,
            "opposing": 1,
            "neutral": 0,
            "not_addressed": 0,
            "consensus_level": "moderate_agreement",
            "average_confidence": 0.85,
            "retracted_sources": 0,
            "cached": True,
            "reproducibility_hash": "test_hash",
        }
        mock_cache.get_evidence_meter = AsyncMock(return_value=cached_data)

        try:
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={
                    "claim": "Test claim",
                    "source_ids": ",".join(sample_source_ids),
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["cached"] is True
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    def test_meter_cross_tenant_source_returns_404_without_classifier(
        self, mock_classifier, test_client, mock_auth
    ):
        db = TestingSessionLocal()
        document = seed_document(db, organization_id=TEST_ORG_ID_B)
        mock_classifier.classify_sources_batch = AsyncMock()

        try:
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={"claim": "Cross tenant claim", "source_ids": str(document.id)},
            )

            assert response.status_code == 404
            assert response_message(response) == "Sources not found or unavailable"
            mock_classifier.classify_sources_batch.assert_not_awaited()
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    def test_meter_missing_source_returns_404_without_classifier(
        self, mock_classifier, test_client, mock_auth
    ):
        mock_classifier.classify_sources_batch = AsyncMock()

        response = test_client.get(
            "/api/v1/evidence/meter",
            params={"claim": "Missing source claim", "source_ids": str(uuid4())},
        )

        assert response.status_code == 404
        assert response_message(response) == "Sources not found or unavailable"
        mock_classifier.classify_sources_batch.assert_not_awaited()

    @patch("src.api.evidence.router.stance_classifier")
    def test_meter_null_org_returns_404_without_classifier(
        self, mock_classifier, test_client, mock_auth
    ):
        set_active_user(MockUser(organization_id=None))
        db = TestingSessionLocal()
        document = seed_document(db, organization_id=TEST_ORG_ID)
        mock_classifier.classify_sources_batch = AsyncMock()

        try:
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={
                    "claim": "Null organization claim",
                    "source_ids": str(document.id),
                },
            )

            assert response.status_code == 404
            assert response_message(response) == "Sources not found or unavailable"
            mock_classifier.classify_sources_batch.assert_not_awaited()
        finally:
            set_active_user(MockUser())
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    def test_meter_mixed_tenant_sources_returns_404_without_classifier(
        self, mock_classifier, test_client, mock_auth
    ):
        db = TestingSessionLocal()
        owned = seed_document(db, organization_id=TEST_ORG_ID)
        foreign = seed_document(db, organization_id=TEST_ORG_ID_B)
        mock_classifier.classify_sources_batch = AsyncMock()

        try:
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={
                    "claim": "Mixed tenant claim",
                    "source_ids": f"{owned.id},{foreign.id}",
                },
            )

            assert response.status_code == 404
            assert response_message(response) == "Sources not found or unavailable"
            mock_classifier.classify_sources_batch.assert_not_awaited()
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    def test_meter_rejects_pending_or_blank_content_readiness(
        self, mock_classifier, test_client, mock_auth
    ):
        mock_classifier.classify_sources_batch = AsyncMock()
        db = TestingSessionLocal()

        try:
            for status_value, content in (
                (ProcessingStatus.PENDING, "Pending document content"),
                (ProcessingStatus.COMPLETED, "   "),
            ):
                document = seed_document(
                    db,
                    processing_status=status_value,
                    content_text=content,
                )
                response = test_client.get(
                    "/api/v1/evidence/meter",
                    params={
                        "claim": "Source readiness claim",
                        "source_ids": str(document.id),
                    },
                )

                assert response.status_code == 409
                assert (
                    response_message(response)
                    == "One or more sources are not ready for evidence analysis"
                )
                mock_classifier.classify_sources_batch.assert_not_awaited()
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    @patch("src.api.evidence.router.consensus_calculator")
    @patch("src.api.evidence.router.cache_service")
    def test_meter_excludes_withdrawn_source_and_tracks_revision(
        self,
        mock_cache,
        mock_consensus,
        mock_classifier,
        test_client,
        mock_auth,
    ):
        from src.api.evidence.schemas import ConsensusLevel, EvidenceMeter

        db = TestingSessionLocal()
        active = seed_document(
            db,
            title="Active source",
            content_text="Active source contains the evidence claim.",
        )
        withdrawn = seed_document(
            db,
            title="Withdrawn source",
            content_text="Withdrawn source contained old evidence.",
            is_deleted=True,
        )
        mock_cache.get_evidence_meter = AsyncMock(return_value=None)
        mock_cache.set_evidence_meter = AsyncMock(return_value=None)
        mock_classifier.model_version = "gpt-4o-mini-2024-07-18"
        mock_classifier.classify_sources_batch = AsyncMock(
            return_value=[
                {
                    "source_id": str(active.id),
                    "stance": "supporting",
                    "confidence": 0.9,
                    "justification_excerpt": "Active source contains the evidence claim.",
                    "model_version": "gpt-4o-mini-2024-07-18",
                    "source_content_hash": active.checksum_sha256,
                }
            ]
        )
        mock_consensus._generate_claim_hash.return_value = "withdrawn_claim_hash"
        mock_consensus.calculate_consensus.return_value = EvidenceMeter(
            claim="Withdrawn source claim",
            claim_hash="withdrawn_claim_hash",
            total_sources=1,
            supporting=1,
            opposing=0,
            neutral=0,
            not_addressed=0,
            consensus_level=ConsensusLevel.INSUFFICIENT_DATA,
            average_confidence=0.9,
            retracted_sources=1,
            cached=False,
            reproducibility_hash="withdrawn_revision_hash",
        )

        try:
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={
                    "claim": "Withdrawn source claim",
                    "source_ids": f"{active.id},{withdrawn.id}",
                },
            )

            assert response.status_code == 200
            assert response.json()["retracted_sources"] == 1
            payloads = mock_classifier.classify_sources_batch.await_args.kwargs[
                "sources"
            ]
            assert [payload["source_id"] for payload in payloads] == [active.id]
            consensus_kwargs = mock_consensus.calculate_consensus.call_args.kwargs
            assert consensus_kwargs["retracted_source_ids"] == [str(withdrawn.id)]
            assert (
                f"{active.id}:{active.checksum_sha256}"
                in consensus_kwargs["source_revisions"]
            )
            assert f"{withdrawn.id}:withdrawn" in consensus_kwargs["source_revisions"]
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    def test_meter_rejects_duplicate_source_ids_without_classifier(
        self, mock_classifier, test_client, mock_auth
    ):
        db = TestingSessionLocal()
        document = seed_document(db)
        mock_classifier.classify_sources_batch = AsyncMock()

        try:
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={
                    "claim": "Duplicate source claim",
                    "source_ids": f"{document.id},{document.id}",
                },
            )

            assert response.status_code == 400
            assert response_message(response) == "Duplicate source IDs are not allowed"
            mock_classifier.classify_sources_batch.assert_not_awaited()
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    @patch("src.api.evidence.router.consensus_calculator")
    @patch("src.api.evidence.router.cache_service")
    def test_meter_commit_failure_returns_500_without_cache_write(
        self,
        mock_cache,
        mock_consensus,
        mock_classifier,
        test_client,
        mock_auth,
    ):
        from src.api.evidence.schemas import ConsensusLevel, EvidenceMeter

        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Persisted source",
            content_text="Persisted source contains the commit failure claim.",
        )
        db.close()

        mock_cache.get_evidence_meter = AsyncMock(return_value=None)
        mock_cache.set_evidence_meter = AsyncMock(return_value=None)
        mock_classifier.model_version = "gpt-4o-mini-2024-07-18"
        mock_classifier.classify_sources_batch = AsyncMock(
            return_value=[
                {
                    "source_id": str(document.id),
                    "stance": "supporting",
                    "confidence": 0.9,
                    "justification_excerpt": "Persisted source contains the commit failure claim.",
                    "model_version": "gpt-4o-mini-2024-07-18",
                    "source_content_hash": document.checksum_sha256,
                }
            ]
        )
        mock_consensus._generate_claim_hash.return_value = "commit_failure_hash"
        mock_consensus.calculate_consensus.return_value = EvidenceMeter(
            claim="Commit failure claim",
            claim_hash="commit_failure_hash",
            total_sources=1,
            supporting=1,
            opposing=0,
            neutral=0,
            not_addressed=0,
            consensus_level=ConsensusLevel.INSUFFICIENT_DATA,
            average_confidence=0.9,
            retracted_sources=0,
            cached=False,
            reproducibility_hash="commit_failure_revision",
        )

        with patch.object(
            Session, "commit", side_effect=RuntimeError("database unavailable")
        ):
            response = test_client.get(
                "/api/v1/evidence/meter",
                params={
                    "claim": "Commit failure claim",
                    "source_ids": str(document.id),
                },
            )

        assert response.status_code == 500
        assert response_message(response) == "Failed to generate evidence meter"
        mock_cache.set_evidence_meter.assert_not_awaited()


class TestEvidenceBreakdownEndpoint:
    """Test /api/v1/evidence/breakdown endpoint"""

    def test_get_evidence_breakdown_success(self, test_client, mock_auth):
        """Test successful evidence breakdown retrieval"""
        set_active_user(MockUser())  # default org
        # Create test stance classifications in database
        db = TestingSessionLocal()

        claim_hash = "test_claim_hash_123"
        source_ids = [uuid4() for _ in range(2)]

        first_document = seed_document(
            db,
            document_id=source_ids[0],
            title="First breakdown source",
        )
        second_document = seed_document(
            db,
            document_id=source_ids[1],
            title="Second breakdown source",
        )

        classifications = [
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=source_ids[0],
                organization_id=TEST_ORG_ID,
                claim_text="Stored breakdown claim",
                source_content_hash=first_document.checksum_sha256,
                stance="supporting",
                confidence=0.90,
                justification_excerpt="Strong evidence",
                model_version=stance_classifier.classifier_version,
            ),
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=source_ids[1],
                organization_id=TEST_ORG_ID,
                claim_text="Stored breakdown claim",
                source_content_hash=second_document.checksum_sha256,
                stance="opposing",
                confidence=0.85,
                justification_excerpt="Contradictory evidence",
                model_version=stance_classifier.classifier_version,
            ),
        ]

        for classification in classifications:
            db.add(classification)
        db.commit()

        try:
            response = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )

            assert response.status_code == 200
            data = response.json()

            assert data["claim_hash"] == claim_hash
            assert data["claim"] == "Stored breakdown claim"
            assert len(data["sources"]) == 2
            assert data["sources"][0]["title"] == "First breakdown source"
            assert data["sources"][0]["publication_retraction_status"] == "unknown"
            assert (
                data["sources"][0]["confidence"] >= data["sources"][1]["confidence"]
            )  # Sorted by confidence

        finally:
            db.close()

    def test_get_evidence_breakdown_with_stance_filter(self, test_client, mock_auth):
        """Test breakdown endpoint with stance filter"""
        set_active_user(MockUser())  # default org
        # Create test data
        db = TestingSessionLocal()

        claim_hash = "test_filter_hash_456"
        source_ids = [uuid4() for _ in range(3)]

        documents = {
            source_id: seed_document(db, document_id=source_id)
            for source_id in source_ids
        }

        classifications = [
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=source_ids[0],
                organization_id=TEST_ORG_ID,
                claim_text="Stored filtered claim",
                source_content_hash=documents[source_ids[0]].checksum_sha256,
                stance="supporting",
                confidence=0.90,
                justification_excerpt="Support 1",
                model_version=stance_classifier.classifier_version,
            ),
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=source_ids[1],
                organization_id=TEST_ORG_ID,
                claim_text="Stored filtered claim",
                source_content_hash=documents[source_ids[1]].checksum_sha256,
                stance="supporting",
                confidence=0.85,
                justification_excerpt="Support 2",
                model_version=stance_classifier.classifier_version,
            ),
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=source_ids[2],
                organization_id=TEST_ORG_ID,
                claim_text="Stored filtered claim",
                source_content_hash=documents[source_ids[2]].checksum_sha256,
                stance="opposing",
                confidence=0.80,
                justification_excerpt="Opposition",
                model_version=stance_classifier.classifier_version,
            ),
        ]

        for classification in classifications:
            db.add(classification)
        db.commit()

        try:
            # Filter for supporting stances only
            response = test_client.get(
                "/api/v1/evidence/breakdown",
                params={"claim_hash": claim_hash, "stance_filter": "supporting"},
            )

            assert response.status_code == 200
            data = response.json()

            assert len(data["sources"]) == 2  # Only supporting sources
            for source in data["sources"]:
                assert source["stance"] == "supporting"

        finally:
            db.close()

    def test_breakdown_pagination_orders_equal_confidence_by_source_id(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        source_ids = [uuid4() for _ in range(3)]
        claim_hash = "equal_confidence_pagination_hash"
        documents = {}

        for source_id in source_ids:
            documents[source_id] = seed_document(
                db,
                document_id=source_id,
                title=f"Source {source_id}",
                content_text="Equal confidence pagination source content",
            )

        for source_id in reversed(source_ids):
            db.add(
                StanceClassificationModel(
                    claim_hash=claim_hash,
                    claim_text="Equal confidence pagination claim",
                    source_id=source_id,
                    organization_id=TEST_ORG_ID,
                    stance="supporting",
                    confidence=0.75,
                    justification_excerpt="Equal confidence pagination source content",
                    source_content_hash=documents[source_id].checksum_sha256,
                    model_version=stance_classifier.classifier_version,
                )
            )
        db.commit()

        try:
            page_ids = []
            for offset in range(len(source_ids)):
                response = test_client.get(
                    "/api/v1/evidence/breakdown",
                    params={
                        "claim_hash": claim_hash,
                        "limit": 1,
                        "offset": offset,
                    },
                )

                assert response.status_code == 200
                page_ids.append(response.json()["sources"][0]["source_id"])

            expected_ids = [str(source_id) for source_id in sorted(source_ids)]
            assert page_ids == expected_ids

            repeat_response = test_client.get(
                "/api/v1/evidence/breakdown",
                params={"claim_hash": claim_hash, "limit": 1, "offset": 1},
            )
            assert repeat_response.status_code == 200
            assert repeat_response.json()["sources"][0]["source_id"] == expected_ids[1]
        finally:
            db.close()

    def test_breakdown_hides_classification_after_document_revision_changes(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        original_content = "Original source content for revision filtering."
        document = seed_document(
            db,
            title="Revised source",
            content_text=original_content,
        )
        claim_hash = "stale_revision_visibility_hash"
        db.add(
            StanceClassificationModel(
                claim_hash=claim_hash,
                claim_text="Revision filtering claim",
                source_id=document.id,
                source_content_hash=document.checksum_sha256,
                organization_id=TEST_ORG_ID,
                stance="supporting",
                confidence=0.95,
                justification_excerpt=original_content,
                model_version=stance_classifier.classifier_version,
            )
        )
        db.commit()

        revised_content = "Changed source content invalidates the old classification."
        uploaded_checksum = document.checksum_sha256
        document.content_text = revised_content
        db.commit()

        try:
            response = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )

            assert response.status_code == 404
            assert (
                response_message(response) == "No classifications found for this claim"
            )
            assert document.checksum_sha256 == uploaded_checksum
        finally:
            db.close()

    def test_breakdown_hides_stale_row_with_null_document_content(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Null-content source",
            content_text="Content removed after classification.",
        )
        claim_hash = "stale_null_content_hash"
        db.add(
            StanceClassificationModel(
                claim_hash=claim_hash,
                claim_text="Null-content revision claim",
                source_id=document.id,
                source_content_hash="old-content-hash",
                organization_id=TEST_ORG_ID,
                stance="supporting",
                confidence=0.9,
                justification_excerpt="Content removed after classification.",
                model_version=stance_classifier.classifier_version,
            )
        )
        db.commit()

        document.checksum_sha256 = None
        document.content_text = None
        db.commit()

        try:
            response = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )

            assert response.status_code == 404
            assert (
                response_message(response) == "No classifications found for this claim"
            )
        finally:
            db.close()

    def test_breakdown_hides_row_when_document_content_is_null_even_with_checksum(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Null-content source with uploaded checksum",
            content_text="Content available when classified.",
        )
        claim_hash = "null_content_uploaded_checksum_hash"
        db.add(
            StanceClassificationModel(
                claim_hash=claim_hash,
                claim_text="Null-content uploaded checksum revision claim",
                source_id=document.id,
                source_content_hash=document.checksum_sha256,
                organization_id=TEST_ORG_ID,
                stance="supporting",
                confidence=0.9,
                justification_excerpt="Content available when classified.",
                model_version=stance_classifier.classifier_version,
            )
        )
        db.commit()

        document.content_text = None
        db.commit()

        try:
            response = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )

            assert response.status_code == 404
            assert (
                response_message(response) == "No classifications found for this claim"
            )
        finally:
            db.close()

    def test_breakdown_stale_candidates_do_not_consume_pagination(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        claim_hash = "stale_candidate_pagination_hash"
        stale_document = seed_document(
            db,
            title="Stale high-confidence source",
            content_text="Original stale source content.",
        )
        current_first = seed_document(
            db,
            title="Current first source",
            content_text="Current first source content.",
        )
        current_second = seed_document(
            db,
            title="Current second source",
            content_text="Current second source content.",
        )

        db.add_all(
            [
                StanceClassificationModel(
                    claim_hash=claim_hash,
                    claim_text="Stale pagination claim",
                    source_id=stale_document.id,
                    source_content_hash=stale_document.checksum_sha256,
                    organization_id=TEST_ORG_ID,
                    stance="supporting",
                    confidence=0.99,
                    justification_excerpt="Original stale source content.",
                    model_version=stance_classifier.classifier_version,
                ),
                StanceClassificationModel(
                    claim_hash=claim_hash,
                    claim_text="Stale pagination claim",
                    source_id=current_first.id,
                    source_content_hash=current_first.checksum_sha256,
                    organization_id=TEST_ORG_ID,
                    stance="supporting",
                    confidence=0.80,
                    justification_excerpt="Current first source content.",
                    model_version=stance_classifier.classifier_version,
                ),
                StanceClassificationModel(
                    claim_hash=claim_hash,
                    claim_text="Stale pagination claim",
                    source_id=current_second.id,
                    source_content_hash=current_second.checksum_sha256,
                    organization_id=TEST_ORG_ID,
                    stance="supporting",
                    confidence=0.70,
                    justification_excerpt="Current second source content.",
                    model_version=stance_classifier.classifier_version,
                ),
            ]
        )
        db.commit()

        revised_content = "Revised stale source content."
        uploaded_checksum = stale_document.checksum_sha256
        stale_document.content_text = revised_content
        db.commit()

        try:
            first_page = test_client.get(
                "/api/v1/evidence/breakdown",
                params={"claim_hash": claim_hash, "limit": 1, "offset": 0},
            )
            second_page = test_client.get(
                "/api/v1/evidence/breakdown",
                params={"claim_hash": claim_hash, "limit": 1, "offset": 1},
            )

            assert first_page.status_code == 200
            assert second_page.status_code == 200
            assert first_page.json()["sources"][0]["source_id"] == str(current_first.id)
            assert second_page.json()["sources"][0]["source_id"] == str(
                current_second.id
            )
            assert stale_document.checksum_sha256 == uploaded_checksum
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_breakdown_pagination_stops_after_enough_current_rows(self):
        db = Mock()
        query = Mock()
        db.query.return_value = query
        query.join.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        first_source_id = uuid4()
        second_source_id = uuid4()
        first_content = "A grounded excerpt"
        second_content = "Another grounded excerpt"
        first_hash = hashlib.sha256(first_content.encode("utf-8")).hexdigest()
        second_hash = hashlib.sha256(second_content.encode("utf-8")).hexdigest()
        rows = [
            (
                first_source_id,
                "A deterministic claim",
                "supporting",
                0.75,
                "A grounded excerpt",
                first_hash,
                "A source",
                first_content,
            ),
            (
                second_source_id,
                "A deterministic claim",
                "supporting",
                0.7,
                "Another grounded excerpt",
                second_hash,
                "Another source",
                second_content,
            ),
        ]

        def candidate_rows():
            yield rows[0]
            yield rows[1]
            raise AssertionError("breakdown consumed beyond the requested page")

        query.yield_per.return_value = candidate_rows()

        await get_evidence_breakdown(
            claim_hash="deterministic_pagination_hash",
            stance_filter=None,
            limit=1,
            offset=1,
            _rate_limit=True,
            current_user=MockUser(),
            db=db,
        )

        assert query.yield_per.called

    def test_get_evidence_breakdown_not_found(self, test_client, mock_auth):
        """Test breakdown endpoint with non-existent claim hash"""
        set_active_user(MockUser())  # default org
        response = test_client.get(
            "/api/v1/evidence/breakdown", params={"claim_hash": "nonexistent_hash"}
        )

        assert response.status_code == 404
        data = response.json()
        detail = data.get("detail") or data.get("error", {}).get("message", "")
        assert "No classifications found" in detail

    def test_breakdown_isolated_by_organization(self, test_client, mock_auth):
        """REGRESSION: /breakdown must only return the caller's org classifications.

        Before the fix, /breakdown was scoped only by claim_hash + model_version
        (a deterministic SHA-256 of public claim text), so an attacker could read
        another org's source_id + justification_excerpt by computing the hash.
        This test fails before the fix (both orgs' rows returned) and passes after.
        """
        db = TestingSessionLocal()
        claim_hash = "shared_claim_hash_tenant_regression"
        model_version = stance_classifier.classifier_version

        org_a_source = uuid4()
        org_b_source = uuid4()
        org_a_excerpt = "ORG_A_SECRET_EXCERPT_DO_NOT_LEAK"
        org_b_excerpt = "ORG_B_SECRET_EXCERPT_DO_NOT_LEAK"

        seed_document(
            db,
            document_id=org_a_source,
            organization_id=TEST_ORG_ID,
            title="Org A source",
            content_text="Org A source content",
        )
        seed_document(
            db,
            document_id=org_b_source,
            organization_id=TEST_ORG_ID_B,
            title="Org B source",
            content_text="Org B source content",
        )

        rows = [
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=org_a_source,
                organization_id=TEST_ORG_ID,
                claim_text="Shared tenant claim",
                source_content_hash=hashlib.sha256(
                    "Org A source content".encode("utf-8")
                ).hexdigest(),
                stance="supporting",
                confidence=0.9,
                justification_excerpt=org_a_excerpt,
                model_version=model_version,
            ),
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=org_b_source,
                organization_id=TEST_ORG_ID_B,
                claim_text="Shared tenant claim",
                source_content_hash=hashlib.sha256(
                    "Org B source content".encode("utf-8")
                ).hexdigest(),
                stance="opposing",
                confidence=0.8,
                justification_excerpt=org_b_excerpt,
                model_version=model_version,
            ),
        ]
        for row in rows:
            db.add(row)
        db.commit()
        db.close()

        try:
            # --- As Org A: must see ONLY Org A's row ---
            set_active_user(MockUser(organization_id=TEST_ORG_ID))
            resp_a = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )
            assert resp_a.status_code == 200, resp_a.text
            sources_a = resp_a.json()["sources"]
            assert len(sources_a) == 1, f"expected 1 source for org A, got {sources_a}"
            assert sources_a[0]["source_id"] == str(org_a_source)
            assert sources_a[0]["justification_excerpt"] == org_a_excerpt
            # Org A must never see Org B's content
            assert all(s["source_id"] != str(org_b_source) for s in sources_a)
            assert org_b_excerpt not in resp_a.text

            # --- As Org B: must see ONLY Org B's row ---
            set_active_user(MockUser(organization_id=TEST_ORG_ID_B))
            resp_b = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )
            assert resp_b.status_code == 200, resp_b.text
            sources_b = resp_b.json()["sources"]
            assert len(sources_b) == 1, f"expected 1 source for org B, got {sources_b}"
            assert sources_b[0]["source_id"] == str(org_b_source)
            assert sources_b[0]["justification_excerpt"] == org_b_excerpt
            assert all(s["source_id"] != str(org_a_source) for s in sources_b)
            assert org_a_excerpt not in resp_b.text

        finally:
            set_active_user(MockUser())  # reset to default org for any later tests

    def test_breakdown_rejects_null_org_user(self, test_client, mock_auth):
        """A user without an org must never see any classifications.

        SQLAlchemy compiles ``Column == None`` to ``IS NULL`` — without an explicit
        guard a NULL-org user would match every NULL-org row (a shared bucket across all
        such users). The endpoint must fail closed instead. Seeds a NULL-org row to prove
        it is not returned even to the NULL-org user themselves.
        """
        db = TestingSessionLocal()
        claim_hash = "null_org_claim_hash_regression"
        model_version = stance_classifier.classifier_version
        secret_excerpt = "NULL_ORG_BUCKET_SHOULD_NOT_LEAK"

        db.add(
            StanceClassificationModel(
                claim_hash=claim_hash,
                source_id=uuid4(),
                organization_id=None,
                stance="supporting",
                confidence=0.9,
                justification_excerpt=secret_excerpt,
                model_version=model_version,
            )
        )
        db.commit()
        db.close()

        try:
            set_active_user(MockUser(organization_id=None))
            resp = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )
            assert resp.status_code == 404
            assert secret_excerpt not in resp.text
        finally:
            set_active_user(MockUser())  # reset to default org

    def test_breakdown_uses_stored_claim_and_real_document_provenance(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Real document title",
            content_text="Grounded source excerpt for the stored claim.",
        )
        claim_hash = "stored_provenance_breakdown_hash"
        db.add(
            StanceClassificationModel(
                claim_hash=claim_hash,
                claim_text="Stored original claim",
                source_id=document.id,
                source_content_hash=document.checksum_sha256,
                organization_id=TEST_ORG_ID,
                stance="supporting",
                confidence=0.91,
                justification_excerpt="Grounded source excerpt for the stored claim.",
                model_version=stance_classifier.classifier_version,
            )
        )
        db.commit()

        try:
            response = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["claim"] == "Stored original claim"
            assert payload["sources"][0]["title"] == "Real document title"
            assert payload["sources"][0]["justification_excerpt"] == (
                "Grounded source excerpt for the stored claim."
            )
            assert "Original claim text" not in response.text
            assert f"Source {document.id}" not in response.text
        finally:
            db.close()

    def test_breakdown_does_not_render_legacy_null_claim_placeholder(
        self, test_client, mock_auth
    ):
        set_active_user(MockUser())
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Legacy source title",
            content_text="Legacy source content",
        )
        claim_hash = "legacy_null_claim_breakdown_hash"
        db.add(
            StanceClassificationModel(
                claim_hash=claim_hash,
                claim_text=None,
                source_id=document.id,
                organization_id=TEST_ORG_ID,
                stance="supporting",
                confidence=0.8,
                justification_excerpt="Legacy source content",
                model_version=stance_classifier.classifier_version,
            )
        )
        db.commit()

        try:
            response = test_client.get(
                "/api/v1/evidence/breakdown", params={"claim_hash": claim_hash}
            )

            assert response.status_code == 404
            assert (
                response_message(response) == "No classifications found for this claim"
            )
            assert "Original claim text" not in response.text
            assert f"Source {document.id}" not in response.text
        finally:
            db.close()

    def test_get_evidence_breakdown_missing_claim_hash(self, test_client, mock_auth):
        """Test breakdown endpoint with missing claim_hash parameter"""
        response = test_client.get("/api/v1/evidence/breakdown")

        assert response.status_code == 422  # Validation error


class TestStanceClassificationPersistence:
    """Test persistence of source and claim provenance."""

    def test_save_stance_classifications_persists_provenance(self, test_client):
        db = TestingSessionLocal()
        source_id = uuid4()
        source_content_hash = "b" * 64

        try:
            saved_count = _save_stance_classifications(
                db=db,
                classifications=[
                    {
                        "source_id": source_id,
                        "stance": "supporting",
                        "confidence": 0.9,
                        "justification_excerpt": "Original claim appears in source",
                        "model_version": "gpt-4o-mini-2024-07-18",
                        "source_content_hash": source_content_hash,
                    }
                ],
                claim_hash="a" * 64,
                claim_text="Original claim",
                classifier_version="classifier-v2",
                organization_id=TEST_ORG_ID,
            )
            db.commit()

            stored = (
                db.query(StanceClassificationModel)
                .filter(StanceClassificationModel.source_id == source_id)
                .one()
            )

            assert saved_count == 1
            assert stored.claim_text == "Original claim"
            assert stored.source_content_hash == source_content_hash
            assert stored.model_version == "classifier-v2"
            assert stored.inference_model_version == "gpt-4o-mini-2024-07-18"

            _save_stance_classifications(
                db=db,
                classifications=[
                    {
                        "source_id": source_id,
                        "stance": "opposing",
                        "confidence": 0.8,
                        "justification_excerpt": "Updated classification",
                        "model_version": "gpt-4o-2024-08-06",
                        "source_content_hash": source_content_hash,
                    }
                ],
                claim_hash="a" * 64,
                claim_text="Original claim",
                classifier_version="classifier-v2",
                organization_id=TEST_ORG_ID,
            )
            db.commit()
            stored = (
                db.query(StanceClassificationModel)
                .filter(StanceClassificationModel.source_id == source_id)
                .one()
            )
            assert stored.inference_model_version == "gpt-4o-2024-08-06"
            assert stored.stance == "opposing"
        finally:
            db.close()


class TestClassifyEndpoint:
    """Test /api/v1/evidence/classify endpoint"""

    @patch("src.api.evidence.router.source_loader.load")
    def test_classify_batch_limit_rejected_before_load(
        self, mock_load, test_client, mock_auth
    ):
        """Oversized classify requests fail before touching the source loader."""
        source_ids = [str(uuid4()) for _ in range(101)]
        assert len(set(source_ids)) == 101

        response = test_client.post(
            "/api/v1/evidence/classify",
            params={"claim": "A claim with enough length"},
            json=source_ids,
        )

        assert response.status_code == 400
        assert (
            response_message(response)
            == "Maximum 100 sources allowed per batch classification request"
        )
        mock_load.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.api.evidence.router.stance_classifier")
    async def test_classify_releases_loader_transaction_before_classifier_await(
        self, mock_classifier
    ):
        """The loader read transaction is closed before classification awaits."""
        db = TestingSessionLocal()
        document = seed_document(
            db,
            title="Transaction boundary classify source",
            content_text="Transaction boundary classify source contains the claim.",
        )
        observed_in_transaction = []

        async def observe_classifier(*args, **kwargs):
            observed_in_transaction.append(db.in_transaction())
            return []

        mock_classifier.model_version = "gpt-4o-mini-2024-07-18"
        mock_classifier.classify_sources_batch = AsyncMock(
            side_effect=observe_classifier
        )

        try:
            result = await classify_sources_for_claim(
                claim="Transaction boundary classify claim",
                source_ids=[document.id],
                current_user=MockUser(),
                db=db,
            )

            assert result["status"] == "completed"
            assert observed_in_transaction == [False]
            assert db.in_transaction() is False
        finally:
            db.close()

    @patch("src.api.evidence.router.stance_classifier")
    @patch("src.api.evidence.router.consensus_calculator")
    def test_classify_sources_real_source_success(
        self, mock_consensus, mock_classifier, test_client, mock_auth
    ):
        """Test successful source classification"""
        set_active_user(MockUser())
        db = TestingSessionLocal()
        seeded_documents = [
            seed_document(
                db,
                title="Classify source one",
                content_text="Classify source one has the original claim evidence.",
            ),
            seed_document(
                db,
                title="Classify source two",
                content_text="Classify source two gives neutral context.",
            ),
        ]
        uuid_source_ids = [str(document.id) for document in seeded_documents]

        # Mock stance classifications
        mock_classifications = [
            {
                "source_id": uuid_source_ids[0],
                "stance": "supporting",
                "confidence": 0.90,
                "justification_excerpt": "Classify source one has the original claim evidence.",
                "model_version": "gpt-4o-mini-2024-07-18",
                "source_content_hash": seeded_documents[0].checksum_sha256,
            },
            {
                "source_id": uuid_source_ids[1],
                "stance": "neutral",
                "confidence": 0.75,
                "justification_excerpt": "Classify source two gives neutral context.",
                "model_version": "gpt-4o-mini-2024-07-18",
                "source_content_hash": seeded_documents[1].checksum_sha256,
            },
        ]
        mock_classifier.classify_sources_batch = AsyncMock(
            return_value=mock_classifications
        )
        mock_classifier.model_version = "gpt-4o-mini-2024-07-18"
        mock_consensus._generate_claim_hash.return_value = "test_hash"

        try:
            response = test_client.post(
                "/api/v1/evidence/classify",
                params={"claim": "Test claim"},
                json=uuid_source_ids,
            )

            assert response.status_code == 201
            data = response.json()

            assert data["status"] == "completed"
            assert data["claim_hash"] == "test_hash"
            assert data["classifications_created"] == 2
            assert data["total_sources"] == 2

            payloads = mock_classifier.classify_sources_batch.await_args.kwargs[
                "sources"
            ]
            assert payloads[0]["excerpt"] in seeded_documents[0].content_text
            assert "Mock excerpt" not in payloads[0]["excerpt"]
            assert payloads[0]["content_hash"] == seeded_documents[0].checksum_sha256

            stored = (
                db.query(StanceClassificationModel)
                .filter(
                    StanceClassificationModel.claim_hash == "test_hash",
                    StanceClassificationModel.source_id == seeded_documents[0].id,
                )
                .one()
            )
            assert stored.claim_text == "Test claim"
            assert stored.source_content_hash == seeded_documents[0].checksum_sha256
        finally:
            db.close()

    def test_classify_sources_rejects_malformed_source_ids_with_400(
        self, test_client, mock_auth
    ):
        """Malformed source IDs follow the evidence API's 400 contract."""
        response = test_client.post(
            "/api/v1/evidence/classify",
            params={"claim": "Test claim"},
            json=["not-a-uuid"],
        )

        assert response.status_code == 400
        assert response_message(response) == "Invalid source ID format"


class TestHealthEndpoint:
    """Test /api/v1/evidence/health endpoint"""

    @patch("src.api.evidence.router.cache_service")
    @patch("src.api.evidence.router.consensus_calculator")
    @patch("src.api.evidence.router.stance_classifier")
    def test_health_check_healthy(
        self, mock_classifier, mock_consensus, mock_cache, test_client
    ):
        """Test healthy service status"""
        # Mock healthy components
        mock_cache._ensure_connected = Mock(return_value=True)
        mock_consensus._generate_claim_hash.return_value = "test_hash"
        mock_classifier.model_version = "gpt-4o-mini-2024-07-18"

        response = test_client.get("/api/v1/evidence/health")

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "healthy"
        assert data["cache_connected"] is True
        assert data["model_version"] == "gpt-4o-mini-2024-07-18"
        assert all(
            component == "operational" or component == "degraded"
            for component in data["components"].values()
        )

    @patch("src.api.evidence.router.cache_service")
    def test_health_check_cache_disconnected(self, mock_cache, test_client):
        """Test service status with cache disconnected"""
        mock_cache._ensure_connected = Mock(return_value=False)

        response = test_client.get("/api/v1/evidence/health")

        assert response.status_code == 200
        data = response.json()

        assert data["cache_connected"] is False
        assert data["components"]["cache_service"] == "degraded"
