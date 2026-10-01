"""Manuscript release contracts (GOO-315). Every POST carries an
``idempotency_key`` and the hashes the caller saw; status is derived on read.
``external_submission`` is constant: packaging never authorizes a submission
(GOO-318 owns deposits)."""

from datetime import datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CheckState = Literal["pass", "fail", "unknown", "not_applicable"]
ReleaseStage = Literal["candidate", "verified"]
ReleaseStatus = Literal["candidate", "verified", "stale"]
_KEY = Field(..., min_length=1, max_length=255)
_HASH = Field(..., min_length=64, max_length=64)


class CandidateCreate(BaseModel):
    """Package one exact saved draft version (bound by its content hash)."""

    draft_id: UUID
    expected_content_hash: str = _HASH
    idempotency_key: str = _KEY


class PromoteRequest(BaseModel):
    """Promote exactly the candidate snapshot and content the caller saw."""

    expected_snapshot_hash: str = _HASH
    expected_content_hash: str = _HASH
    idempotency_key: str = _KEY


class CheckItem(BaseModel):
    code: str
    detail: str
    ref: Optional[str] = None


class CheckResult(BaseModel):
    state: CheckState
    items: List[CheckItem] = Field(default_factory=list)


class PackageFile(BaseModel):
    path: str
    sha256: str
    bytes: int


class ManuscriptReleaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    draft_id: UUID
    draft_version: int
    content_hash: str
    stage: ReleaseStage
    status: ReleaseStatus = "candidate"
    stale_cause: Optional[str] = None
    candidate_release_id: Optional[UUID] = None
    draft_release_id: Optional[UUID] = None
    snapshot_hash: str
    checks: Dict[str, CheckResult]
    checks_hash: str
    failing_obligations: List[str] = Field(default_factory=list)
    package_files: List[PackageFile]
    package_sha256: str
    created_by_id: UUID
    actor_role: str
    created_at: datetime
    external_submission: Literal["not_authorized"] = "not_authorized"


class ManuscriptReleaseListResponse(BaseModel):
    releases: List[ManuscriptReleaseResponse]


class MemberCheck(BaseModel):
    path: str
    sha256: str
    ok: bool


class ReferenceMapping(BaseModel):
    key: str
    title: Optional[str] = None
    entry_sha256: str


class ReleaseVerification(BaseModel):
    """Recomputed from the stored bytes alone; nothing is re-resolved."""

    release_id: UUID
    package_sha256: str
    package_sha256_ok: bool
    members: List[MemberCheck]
    bundle_ok: bool
    bundle_error: Optional[str] = None
    references_ok: bool
    reference_mapping: List[ReferenceMapping]
