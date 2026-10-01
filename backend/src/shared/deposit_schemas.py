"""Archive deposit contracts (GOO-318): Zenodo sandbox deposits of one exact
verified manuscript release.

``status`` is derived from the attempt chain on every read. ``doi`` and
``doi_url`` are shown only once the read-back verified them; an unpublished
draft or a partial upload never reads as published. No response carries a
credential.
"""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_KEY = Field(..., min_length=1, max_length=255)
_HASH = Field(..., min_length=64, max_length=64)
DepositStatus = Literal[
    "prepared",
    "draft_created",
    "files_uploaded",
    "published",
    "verified",
    "failed",
    "ambiguous",
]


class DepositApprovalCreate(BaseModel):
    """Approve depositing this exact release package (RELEASE)."""

    release_id: UUID
    package_sha256: str = _HASH
    rationale: str = Field(..., min_length=1, max_length=2000)
    idempotency_key: str = _KEY


class DepositApprovalRevoke(BaseModel):
    rationale: str = Field(..., min_length=1, max_length=2000)
    idempotency_key: str = _KEY


class DepositApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    release_id: UUID
    package_sha256: str
    repository: str
    account_ref: str
    action: str
    kind: Literal["approved", "revoked"]
    approved_by_id: UUID
    actor_role: str
    rationale: Optional[str] = None
    created_at: datetime
    in_force: bool = False


class DepositCreate(BaseModel):
    """Request the deposit of the release's exact package (RELEASE)."""

    release_id: UUID
    package_sha256: str = _HASH
    idempotency_key: str = _KEY


class DepositFile(BaseModel):
    name: str
    sha256: str
    md5: str
    bytes: int


class DepositAttemptResponse(BaseModel):
    """One phase attempt. Remote DOIs are withheld here (see ``doi``)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    phase: str
    outcome: Literal["succeeded", "failed", "unknown"]
    retryable: bool
    remote_deposition_id: Optional[str] = None
    remote_record_id: Optional[str] = None
    reason: Optional[str] = None
    previous_id: Optional[UUID] = None
    created_at: datetime
    mismatch: List[str] = Field(default_factory=list)


class DepositResponse(BaseModel):
    operation_id: UUID
    release_id: UUID
    package_sha256: str
    repository: str
    account_ref: str
    requested_by_id: UUID
    approval_id: Optional[UUID] = None
    approval_in_force: bool = False
    status: DepositStatus
    last_reason: Optional[str] = None
    files: List[DepositFile]
    remote_deposition_id: Optional[str] = None
    remote_record_id: Optional[str] = None
    doi: Optional[str] = None
    doi_url: Optional[str] = None
    queue_status: Optional[str] = None
    attempts: List[DepositAttemptResponse]
    created_at: datetime


class DepositListResponse(BaseModel):
    repository: Literal["zenodo_sandbox"] = "zenodo_sandbox"
    configured: bool
    account_ref: Optional[str] = None
    approvals: List[DepositApprovalResponse]
    deposits: List[DepositResponse]
