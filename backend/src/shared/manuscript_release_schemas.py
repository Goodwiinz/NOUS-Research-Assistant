"""Manuscript release contracts (GOO-315). Every POST carries an
``idempotency_key`` and the hashes the caller saw; status is derived on read.
``external_submission``: packaging never authorizes a submission; only a
GOO-318 deposit approval in force for the release's exact package makes it
``authorized``. GOO-316 adds the ``anonymized`` package variant:
``package_files`` stays the identified variant's members."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CheckState = Literal["pass", "fail", "unknown", "not_applicable"]
ReleaseStage = Literal["candidate", "verified"]
ReleaseStatus = Literal["candidate", "verified", "stale"]
PackageVariant = Literal["identified", "anonymized"]
ReferenceFormat = Literal["bibtex", "csl-json", "ris"]
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
    anonymized_files: Optional[List[PackageFile]] = None
    anonymized_sha256: Optional[str] = None
    created_by_id: UUID
    actor_role: str
    created_at: datetime
    external_submission: Literal["not_authorized", "authorized"] = "not_authorized"

    @field_validator("package_files", mode="before")
    @classmethod
    def _identified(cls, value: Any) -> Any:
        """GOO-316 rows store ``{identified, anonymized, ...}``."""
        return value["identified"] if isinstance(value, dict) else value


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


class ReferenceOmission(BaseModel):
    """A field a reference file leaves out because the record lacks it
    (``absent``), or a record exported as a generic type (``type_unmapped``,
    with the raw ``value``). GOO-317: never filled in or guessed."""

    key: str
    field: str
    reason: Literal["absent", "type_unmapped"]
    value: Optional[str] = None


class ReferenceReport(BaseModel):
    """``?report=true``: what a release reference file would contain."""

    format: ReferenceFormat
    records: int
    omissions: List[ReferenceOmission]
