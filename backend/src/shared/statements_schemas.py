"""Statement set, approval, ORCID and venue-check contracts (GOO-316).

A ``null`` statement field means *explicitly missing*: it is stored as
``null`` and reported in ``missing_fields``, never filled in. ORCID status is
derived on read (``authenticated`` needs a retained OAuth receipt for the
author's linked user). Authorship grants no project permission.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_KEY = Field(..., min_length=1, max_length=255)
_HASH = Field(..., min_length=64, max_length=64)
OrcidStatus = Literal["authenticated", "unauthenticated", "unknown"]
ApprovalMethod = Literal["in_app_self", "recorded_attestation"]
RuleState = Literal["pass", "fail", "unknown"]


class AuthorIn(BaseModel):
    author_key: str = Field(..., min_length=1, max_length=64)
    order: int = Field(..., ge=1)
    display_name: Optional[str] = None
    affiliations: Optional[List[str]] = None
    email: Optional[str] = None
    corresponding: bool = False
    user_id: Optional[UUID] = None
    orcid: Optional[str] = None
    credit_roles: Optional[List[str]] = None


class GrantIn(BaseModel):
    funder: Optional[str] = None
    award_id: Optional[str] = None


class FundingIn(BaseModel):
    text: Optional[str] = None
    grants: Optional[List[GrantIn]] = None


class LicensesIn(BaseModel):
    text: Optional[str] = None
    data: Optional[str] = None
    code: Optional[str] = None


class StatementBody(BaseModel):
    """``credit_roles`` are ``credit/1`` slugs; ``licenses.text`` an SPDX id."""

    authors: Optional[List[AuthorIn]] = None
    funding: Optional[FundingIn] = None
    conflicts: Optional[str] = None
    ethics: Optional[str] = None
    limitations: Optional[str] = None
    data_availability: Optional[str] = None
    code_availability: Optional[str] = None
    licenses: Optional[LicensesIn] = None


class StatementSetCreate(BaseModel):
    """A new version; ``supersedes_set_id`` must be the current tip."""

    body: StatementBody
    supersedes_set_id: Optional[UUID] = None
    idempotency_key: str = _KEY


class ApprovalCreate(BaseModel):
    """``in_app_self`` only by the author's own linked user;
    ``recorded_attestation`` records an off-platform approval with a note."""

    author_key: str = Field(..., min_length=1, max_length=64)
    set_hash: str = _HASH
    method: ApprovalMethod
    attestation_note: Optional[str] = Field(None, max_length=5000)
    idempotency_key: str = _KEY


class VenueCheckCreate(BaseModel):
    idempotency_key: str = _KEY


class VenueItem(BaseModel):
    rule: str
    field: str
    detail: str
    fix: str


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    statement_set_id: UUID
    author_key: str
    set_hash: str
    method: ApprovalMethod
    approved_by_id: UUID
    attestation_note: Optional[str] = None
    created_at: datetime


class OrcidReceipt(BaseModel):
    authentication_id: UUID
    environment: str
    token_received_at: datetime


class AuthorIdentity(BaseModel):
    author_key: str
    order: int
    display_name: Optional[str] = None
    user_id: Optional[UUID] = None
    orcid: Optional[str] = None
    orcid_status: OrcidStatus
    orcid_receipt: Optional[OrcidReceipt] = None
    approval: Optional[ApprovalResponse] = None


class StatementSetResponse(BaseModel):
    id: UUID
    collection_id: UUID
    body: Dict[str, Any]
    set_hash: str
    schema_id: str
    credit_vocabulary: str
    supersedes_set_id: Optional[UUID] = None
    created_by_id: UUID
    created_at: datetime
    is_tip: bool
    missing_fields: List[str]
    missing_items: List[VenueItem]
    authors: List[AuthorIdentity]
    approvals: List[ApprovalResponse]
    all_approved: bool
    grants_permissions: Literal[False] = False


class StatementsListResponse(BaseModel):
    tip: Optional[StatementSetResponse] = None
    history: List[StatementSetResponse]
    credit_roles: List[str]
    credit_vocabulary: str


class VenueCheckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collection_id: UUID
    release_id: UUID
    profile_id: str
    profile_version: int
    package_sha256: str
    anonymized_sha256: Optional[str] = None
    status: Literal["pass", "fail"]
    rules: Dict[str, RuleState] = Field(default_factory=dict)
    items: List[VenueItem] = Field(default_factory=list)
    checked_by_id: UUID
    created_at: datetime


class VenueCheckListResponse(BaseModel):
    checks: List[VenueCheckResponse]


class OrcidStartResponse(BaseModel):
    authorize_url: str


class OrcidAuthenticationResponse(BaseModel):
    """A non-secret receipt: no access, refresh or ID token, ever."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    orcid: str
    environment: str
    scope: str
    name_claim: Optional[str] = None
    client_id: str
    token_received_at: datetime
    created_at: datetime


class OrcidAuthenticationListResponse(BaseModel):
    authentications: List[OrcidAuthenticationResponse]
