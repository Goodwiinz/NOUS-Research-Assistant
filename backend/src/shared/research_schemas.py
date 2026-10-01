"""
Research Assistant schemas for citation management, citation graphs, research projects, and literature review generation.

This module contains Pydantic schemas for the Research Assistant feature (User Stories 1-5).
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field, validator

# ============================================================================
# Enums
# ============================================================================


class ProjectType(str, Enum):
    """Research project types"""

    RESEARCH = "research"
    LITERATURE_REVIEW = "literature_review"
    THESIS = "thesis"
    PAPER = "paper"


class ResearchStatus(str, Enum):
    """Research project status"""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CitationRelationshipType(str, Enum):
    """Citation relationship types for graph edges"""

    CITES = "cites"
    CITED_BY = "cited_by"
    RELATED_TO = "related_to"


class MetadataSource(str, Enum):
    """Sources for citation metadata extraction"""

    ARXIV = "arxiv"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    CROSSREF = "crossref"
    PDF = "pdf"
    MANUAL = "manual"


class CitationVerdict(str, Enum):
    """CiteCheck-style faithfulness verdicts for draft citations."""

    EXACT = "exact"  # claim fully supported by the source
    MINOR = "minor"  # supported with small imprecision/overstatement
    MAJOR = "major"  # unsupported, contradicted, or identity mismatch
    UNVERIFIED = "unverified"  # provider outage / LLM timeout — not a miss


# ============================================================================
# T019: Citation Schemas (User Story 2)
# ============================================================================


class CitationAuthor(BaseModel):
    """Author information for citations"""

    name: str = Field(..., description="Author's full name")
    affiliation: Optional[str] = Field(
        None, description="Author's institution/affiliation"
    )


def _normalize_author_names(
    authors: Optional[List[Union[str, Dict[str, Any]]]],
) -> Optional[List[str]]:
    """Normalize mixed author payloads into a clean list of names."""
    if authors is None:
        return None

    normalized: List[str] = []
    for author in authors:
        name = ""
        if isinstance(author, str):
            name = author.strip()
        elif isinstance(author, dict):
            # Support both {"name": "..."} and {"given": "...", "family": "..."} payloads.
            if author.get("name"):
                name = str(author["name"]).strip()
            else:
                given = str(author.get("given", "")).strip()
                family = str(author.get("family", "")).strip()
                name = " ".join(part for part in [given, family] if part).strip()
        elif author is not None:
            name = str(author).strip()

        if name:
            normalized.append(name)

    return normalized


class CitationCreate(BaseModel):
    """Create a new citation"""

    message_id: Optional[UUID] = Field(None, description="Associated message ID")
    document_id: Optional[UUID] = Field(None, description="Associated document ID")
    external_reference_id: Optional[str] = Field(
        None, description="External reference identifier"
    )
    document_title: Optional[str] = Field(None, description="Document/paper title")
    document_type: Optional[str] = Field(
        default="paper", description="Type of document"
    )

    # Chunk information
    chunk_index: Optional[int] = None
    chunk_id: Optional[str] = None
    snippet: Optional[str] = None
    page_number: Optional[int] = None

    # Relevance scores
    score: Optional[float] = None
    rerank_score: Optional[float] = None

    # Scholarly metadata
    authors: Optional[List[str]] = Field(default=None, description="List of authors")
    year: Optional[int] = Field(None, ge=1900, le=2100, description="Publication year")
    venue: Optional[str] = Field(None, description="Journal or conference name")
    doi: Optional[str] = Field(None, description="Digital Object Identifier")
    arxiv_id: Optional[str] = Field(None, description="arXiv identifier")
    abstract: Optional[str] = Field(None, description="Paper abstract")
    metadata_source: Optional[str] = Field(
        default="manual", description="Source of metadata"
    )
    needs_review: bool = Field(
        default=False, description="Flag for incomplete metadata"
    )

    @validator("authors", pre=True)
    def normalize_authors(
        cls, value: Optional[List[Union[str, Dict[str, Any]]]]
    ) -> Optional[List[str]]:
        return _normalize_author_names(value)


class CitationResponse(BaseModel):
    """Citation response with all metadata"""

    id: UUID
    message_id: Optional[UUID] = None
    document_id: Optional[UUID] = None
    external_reference_id: Optional[str] = None
    documentTitle: Optional[str] = Field(None, alias="document_title")
    documentType: Optional[str] = Field(None, alias="document_type")

    # Chunk information
    chunkIndex: Optional[int] = Field(None, alias="chunk_index")
    chunkId: Optional[str] = Field(None, alias="chunk_id")
    snippet: Optional[str] = None
    pageNumber: Optional[int] = Field(None, alias="page_number")

    # Relevance scores
    score: Optional[float] = None
    rerankScore: Optional[float] = Field(None, alias="rerank_score")

    # Scholarly metadata
    authors: Optional[List[str]] = Field(default=None, description="List of authors")
    year: Optional[int] = Field(None, ge=1900, le=2100, description="Publication year")
    venue: Optional[str] = Field(None, description="Journal or conference name")
    doi: Optional[str] = Field(None, description="Digital Object Identifier")
    arxivId: Optional[str] = Field(
        None, alias="arxiv_id", description="arXiv identifier"
    )
    abstract: Optional[str] = Field(None, description="Paper abstract")
    metadataSource: Optional[str] = Field(
        None, alias="metadata_source", description="Source of metadata"
    )
    needsReview: bool = Field(
        False, alias="needs_review", description="Flag for incomplete metadata"
    )

    createdAt: datetime = Field(..., alias="created_at")
    updatedAt: datetime = Field(..., alias="updated_at")

    @validator("authors", pre=True)
    def normalize_authors(
        cls, value: Optional[List[Union[str, Dict[str, Any]]]]
    ) -> Optional[List[str]]:
        return _normalize_author_names(value)

    class Config:
        from_attributes = True
        populate_by_name = True


class CitationListResponse(BaseModel):
    """Paginated list of citations"""

    citations: List[CitationResponse]
    total: int
    skip: int
    limit: int


class CitationWithMetadata(BaseModel):
    """Enhanced citation with scholarly metadata"""

    id: UUID
    message_id: UUID
    document_id: Optional[UUID] = None
    external_reference_id: Optional[str] = None
    document_title: Optional[str] = None
    document_type: Optional[str] = None

    # Chunk information
    chunk_index: Optional[int] = None
    chunk_id: Optional[str] = None
    snippet: Optional[str] = None
    page_number: Optional[int] = None

    # Relevance scores
    score: Optional[float] = None
    rerank_score: Optional[float] = None

    # Scholarly metadata
    authors: Optional[List[str]] = Field(default=None, description="List of authors")
    year: Optional[int] = Field(None, ge=1900, le=2100, description="Publication year")
    venue: Optional[str] = Field(None, description="Journal or conference name")
    doi: Optional[str] = Field(None, description="Digital Object Identifier")
    arxiv_id: Optional[str] = Field(
        None, description="arXiv identifier (e.g., 2512.14313v1)"
    )
    abstract: Optional[str] = Field(None, description="Paper abstract")
    metadata_source: Optional[MetadataSource] = Field(
        None, description="Source of metadata"
    )
    needs_review: bool = Field(
        default=False, description="Flag for incomplete metadata"
    )

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @validator("authors", pre=True)
    def normalize_authors(
        cls, value: Optional[List[Union[str, Dict[str, Any]]]]
    ) -> Optional[List[str]]:
        return _normalize_author_names(value)


class CitationExtraction(BaseModel):
    """Request to extract citation metadata from external sources"""

    citation_id: UUID = Field(..., description="Citation ID to enrich with metadata")
    source: Optional[MetadataSource] = Field(
        None, description="Preferred metadata source (auto-detect if None)"
    )
    identifier: Optional[str] = Field(
        None, description="DOI, arXiv ID, or other identifier to use for lookup"
    )


class CitationUpdate(BaseModel):
    """Update citation metadata (partial updates allowed)"""

    authors: Optional[List[str]] = None
    year: Optional[int] = Field(None, ge=1900, le=2100)
    venue: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    abstract: Optional[str] = None
    metadata_source: Optional[MetadataSource] = None
    needs_review: Optional[bool] = None

    @validator("authors", pre=True)
    def normalize_authors(
        cls, value: Optional[List[Union[str, Dict[str, Any]]]]
    ) -> Optional[List[str]]:
        return _normalize_author_names(value)


# ============================================================================
# T020: Citation Graph Schemas (User Story 3)
# ============================================================================


class CitationNode(BaseModel):
    """Node in the citation graph (represents a citation)"""

    id: str = Field(..., description="Citation ID (UUID as string)")
    title: str = Field(..., description="Document/paper title")
    authors: Optional[List[str]] = Field(
        default=None, description="List of author names"
    )
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    citation_count: Optional[int] = Field(
        default=0, description="Number of citations to this paper"
    )

    class Config:
        from_attributes = True


class CitationEdge(BaseModel):
    """Edge in the citation graph (represents a relationship between citations)"""

    id: str = Field(..., description="Relationship ID (UUID as string)")
    source: str = Field(..., description="Source citation ID")
    target: str = Field(..., description="Target citation ID")
    type: CitationRelationshipType = Field(default=CitationRelationshipType.CITES)
    context: Optional[str] = Field(
        None, description="Where the citation appears in text"
    )
    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Extraction confidence"
    )

    class Config:
        from_attributes = True


class CitationGraphRequest(BaseModel):
    """Request to retrieve citation graph for a project"""

    project_id: UUID = Field(..., description="Research project ID")
    max_depth: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Maximum depth of citation relationships to traverse",
    )
    min_confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Minimum confidence score for edges"
    )
    relationship_types: Optional[List[CitationRelationshipType]] = Field(
        default=None, description="Filter by relationship types (None = all types)"
    )


class CitationGraphResponse(BaseModel):
    """Citation graph response with nodes and edges"""

    project_id: UUID
    nodes: List[CitationNode] = Field(
        ..., description="Citations (papers) in the graph"
    )
    edges: List[CitationEdge] = Field(
        ..., description="Relationships between citations"
    )
    total_nodes: int = Field(..., description="Total number of nodes")
    total_edges: int = Field(..., description="Total number of edges")
    depth: int = Field(..., description="Actual depth of the graph")

    class Config:
        from_attributes = True


class CitationRelationshipCreate(BaseModel):
    """Create a new citation relationship."""

    source_citation_id: UUID = Field(
        ..., description="Source citation ID (the citing paper)"
    )
    target_citation_id: UUID = Field(
        ..., description="Target citation ID (the cited paper)"
    )
    relationship_type: CitationRelationshipType = Field(
        default=CitationRelationshipType.CITES,
        # R6-L9: free strings let NaN-ish garbage reach PG and the Neo4j UI.
        description="Typed relationship",
    )
    citation_context: Optional[str] = Field(
        None, max_length=4000, description="Text context where citation appears"
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,  # R6-L9
        description="Confidence score",
    )


class CitationRelationshipResponse(BaseModel):
    """Citation relationship response."""

    id: str
    source_citation_id: str
    target_citation_id: str
    relationship_type: str
    citation_context: Optional[str] = None
    confidence: float = 1.0
    created_at: Optional[datetime] = None


class GraphNodePosition(BaseModel):
    """Position coordinates for graph node."""

    x: float
    y: float


class GraphNode(BaseModel):
    """Enhanced graph node with position for Cytoscape.js."""

    id: str
    title: Optional[str] = None
    authors: Optional[List[str]] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    document_id: Optional[str] = None
    is_uploaded: bool = True
    citation_count: int = 0
    position: Optional[GraphNodePosition] = None
    influence_score: Optional[float] = None


class GraphEdge(BaseModel):
    """Graph edge for Cytoscape.js."""

    id: str
    source: str
    target: str
    type: str = "CITES"
    confidence: float = 1.0


class GraphMetadata(BaseModel):
    """Metadata about the graph response."""

    total_nodes: int
    total_edges: int
    depth: int
    include_external: bool


class CitationGraphData(BaseModel):
    """Full citation graph response for Cytoscape.js visualization."""

    nodes: List[GraphNode]
    edges: List[GraphEdge]
    metadata: GraphMetadata


class GraphNodeDetails(BaseModel):
    """Detailed information about a single graph node."""

    id: str
    document_id: Optional[str] = None
    title: Optional[str] = None
    authors: Optional[List[str]] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    is_uploaded: bool = True
    cited_by: int = 0
    cites: int = 0
    influence_score: float = 0.0


# ============================================================================
# T021: Research Project Schemas (User Story 4)
# ============================================================================


class ProjectCreate(BaseModel):
    """Create a new research project"""

    workspace_id: UUID = Field(..., description="Parent workspace ID")
    name: str = Field(..., min_length=1, max_length=255, description="Project name")
    description: Optional[str] = Field(None, description="Project description")
    project_type: ProjectType = Field(default=ProjectType.RESEARCH)
    research_status: ResearchStatus = Field(default=ResearchStatus.ACTIVE)
    research_goals: Optional[str] = Field(
        None, description="Project objectives and goals"
    )
    deadline: Optional[datetime] = Field(None, description="Project deadline")
    tags: List[str] = Field(default_factory=list, description="Categorization tags")
    color: Optional[str] = Field(
        None, pattern="^#[0-9A-Fa-f]{6}$", description="Hex color for UI"
    )
    icon: Optional[str] = Field(None, max_length=50, description="Icon name for UI")
    is_private: bool = Field(
        default=True, description="Privacy setting (always TRUE for Phase 3)"
    )


class ProjectUpdate(BaseModel):
    """Update research project (partial updates allowed)"""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    project_type: Optional[ProjectType] = None
    research_status: Optional[ResearchStatus] = None
    research_goals: Optional[str] = None
    deadline: Optional[datetime] = None
    tags: Optional[List[str]] = None
    color: Optional[str] = Field(None, pattern="^#[0-9A-Fa-f]{6}$")
    icon: Optional[str] = Field(None, max_length=50)


class ProjectResponse(BaseModel):
    """Research project response; id is the canonical Collection identifier."""

    id: UUID
    research_engine_project_id: Optional[UUID] = None
    can_edit: bool = False
    can_manage: bool = False
    workspace_archived: bool = False
    workspace_id: UUID
    name: str
    description: Optional[str] = None
    project_type: ProjectType
    research_status: ResearchStatus
    research_goals: Optional[str] = None
    deadline: Optional[datetime] = None
    tags: List[str]
    color: Optional[str] = None
    icon: Optional[str] = None
    is_private: bool
    document_count: int = Field(default=0, description="Number of documents in project")
    note_count: int = Field(default=0, description="Number of notes in project")
    draft_count: int = Field(default=0, description="Number of drafts in project")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectNoteCreate(BaseModel):
    """Create a new project note"""

    project_id: Optional[UUID] = Field(
        None,
        description="Parent project ID (optional when provided in route path)",
    )
    title: str = Field(..., min_length=1, max_length=255)
    content: str = Field(
        ..., max_length=200_000, description="Markdown content (bounded, R6-L7)"
    )
    linked_document_ids: List[UUID] = Field(default_factory=list, max_length=100)
    tags: List[str] = Field(default_factory=list, max_length=30)
    is_pinned: bool = Field(default=False)


class ProjectNoteUpdate(BaseModel):
    """Update project note (partial updates allowed)"""

    title: Optional[str] = Field(None, min_length=1, max_length=255)
    content: Optional[str] = Field(None, max_length=200_000)
    linked_document_ids: Optional[List[UUID]] = Field(None, max_length=100)
    tags: Optional[List[str]] = Field(None, max_length=30)
    is_pinned: Optional[bool] = None


class ProjectNoteResponse(BaseModel):
    """Project note response"""

    id: UUID
    project_id: UUID
    user_id: UUID
    title: str
    content: str
    content_preview: str
    linked_document_ids: List[UUID]
    linked_document_count: int
    tags: List[str]
    is_pinned: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectDetailResponse(ProjectResponse):
    """Detailed project response with additional context."""

    documents: List[Dict[str, Any]] = Field(
        default_factory=list, description="Documents in project"
    )
    notes: List[ProjectNoteResponse] = Field(
        default_factory=list, description="Notes in project"
    )
    recent_notes: List[Dict[str, Any]] = Field(
        default_factory=list, description="Recent notes"
    )
    recent_drafts: List[Dict[str, Any]] = Field(
        default_factory=list, description="Recent drafts"
    )


class ProjectListResponse(BaseModel):
    """Paginated list of projects."""

    projects: List[ProjectResponse]
    total: int
    page: int
    size: int
    has_next: bool
    has_prev: bool


class NoteListResponse(BaseModel):
    """Paginated list of notes."""

    notes: List[ProjectNoteResponse]
    total: int
    page: int
    size: int


# Aliases for backwards compatibility
NoteCreate = ProjectNoteCreate
NoteUpdate = ProjectNoteUpdate
NoteResponse = ProjectNoteResponse


# ============================================================================
# Project Memory Schemas (project-scoped persistent memory)
# ============================================================================


class ProjectMemoryCreate(BaseModel):
    """Request to save a durable fact for a project."""

    content: str = Field(..., min_length=1, max_length=2000)
    source: str = Field(default="manual", max_length=32)


class ProjectMemoryResponse(BaseModel):
    """A single project memory."""

    id: UUID
    project_id: UUID
    user_id: UUID
    content: str
    source: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectMemoryListResponse(BaseModel):
    """List of project memories (small, unpaginated)."""

    memories: List[ProjectMemoryResponse]
    total: int


# ============================================================================
# T022: Draft Schemas (User Story 5)
# ============================================================================


class DraftGenerateRequest(BaseModel):
    """Request to generate a new literature review draft"""

    project_id: UUID = Field(..., description="Research project ID")
    title: str = Field(..., min_length=1, max_length=255, description="Draft title")
    themes: List[str] = Field(
        default_factory=list, description="Themes/topics to focus on"
    )
    generation_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Generation parameters (model, temperature, max_tokens, etc.)",
    )
    max_citations: int = Field(
        default=50, ge=1, le=200, description="Maximum number of citations to include"
    )


class DraftCitationData(BaseModel):
    """Citation data within a draft"""

    citation_index: int = Field(..., ge=1, description="Order of citation in the draft")
    document_id: Optional[UUID] = None
    citation_id: Optional[UUID] = None
    snippet: Optional[str] = None
    context: Optional[str] = None


class DraftResponse(BaseModel):
    """Generated draft response"""

    id: UUID
    project_id: UUID
    version: int
    title: str
    content: str
    content_preview: str
    themes: List[str]
    word_count: Optional[int] = None
    citation_count: Optional[int] = None
    generation_params: Optional[Dict[str, Any]] = None
    generation_time_ms: Optional[int] = None
    is_current: bool
    citations: Optional[List[DraftCitationData]] = Field(
        default=None, description="Citations in this draft"
    )
    created_at: datetime

    class Config:
        from_attributes = True


class ReleaseBlocker(BaseModel):
    """One reason a draft version cannot be promoted (GOO-307)."""

    code: str
    claim_version_id: Optional[UUID] = None
    start: int
    end: int
    text: str
    detail: str


class DraftReleaseResponse(BaseModel):
    """A promotion of one exact draft version; stale rows are kept."""

    id: UUID
    collection_id: UUID
    draft_id: UUID
    draft_version: int
    content_hash: str
    claim_version_ids: List[UUID]
    assessment_ids: List[UUID]
    interpretation_claim_version_ids: List[UUID]
    protocol_version_id: Optional[UUID] = None
    policy_version: int
    promoted_by_id: UUID
    actor_role: str
    rationale: Optional[str] = None
    created_at: datetime
    stale_at: Optional[datetime] = None
    stale_event_id: Optional[UUID] = None

    class Config:
        from_attributes = True


class ReleaseInvalidation(BaseModel):
    """Why the latest release is stale: persisted, or derived on read."""

    stale_at: Optional[datetime] = None
    cause: Dict[str, Any]
    changed_nodes: List[str]
    assessment_ids: List[UUID]


class ReleaseCheckResponse(BaseModel):
    """A draft version's release status, blockers and invalidation report."""

    draft_id: UUID
    draft_version: int
    release_status: Literal["candidate", "verified", "stale"]
    content_hash: str
    blockers: List[ReleaseBlocker]
    dimensions: Dict[str, Any]
    release: Optional[DraftReleaseResponse] = None
    invalidation: Optional[ReleaseInvalidation] = None


class DraftPromoteRequest(BaseModel):
    """Promote the exact version whose content hashes to ``content_hash``."""

    content_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    rationale: Optional[str] = Field(default=None, max_length=2000)


class DraftVersion(BaseModel):
    """Draft version metadata (lightweight, for listing versions)"""

    id: UUID
    version: int
    title: str
    word_count: Optional[int] = None
    citation_count: Optional[int] = None
    themes: List[str]
    is_current: bool
    created_at: datetime

    class Config:
        from_attributes = True


class DraftVersionListResponse(BaseModel):
    """List of draft versions for a project"""

    project_id: UUID
    versions: List[DraftVersion]
    total_versions: int
    current_version: Optional[int] = None


# ============================================================================
# Project-Chat Integration Schemas (Phase 2)
# ============================================================================


class StartChatFromProjectRequest(BaseModel):
    """Request to start a new chat from a project with document context"""

    initial_message: str = Field(
        ..., min_length=1, description="First message in the chat"
    )
    conversation_id: Optional[UUID] = Field(
        None, description="Use existing conversation (optional)"
    )
    thread_title: Optional[str] = Field(
        None, max_length=500, description="Custom thread title"
    )


class StartChatFromProjectResponse(BaseModel):
    """Response from starting a chat from a project"""

    thread_id: UUID
    conversation_id: UUID
    project_thread_id: UUID
    document_scope: List[UUID] = Field(
        ..., description="Document IDs included in RAG context"
    )

    class Config:
        from_attributes = True


class LinkThreadRequest(BaseModel):
    """Request to link an existing thread to a project"""

    thread_id: UUID = Field(..., description="Thread ID to link")
    context_note: Optional[str] = Field(
        None, description="Optional note about why this thread is linked"
    )


class ProjectThreadResponse(BaseModel):
    """Response for a project-thread link"""

    id: UUID
    project_id: UUID
    thread_id: UUID
    thread_title: str
    conversation_id: UUID
    link_type: str
    linked_at: datetime
    linked_by_id: Optional[UUID] = None
    context_note: Optional[str] = None
    message_count: int = Field(default=0, description="Number of messages in thread")
    last_message_at: Optional[datetime] = Field(
        None, description="Last message timestamp"
    )

    class Config:
        from_attributes = True


class ProjectThreadListResponse(BaseModel):
    """List of threads linked to a project"""

    threads: List[ProjectThreadResponse]
    total: int


class SaveThreadToNoteRequest(BaseModel):
    """Request to save thread content to a project note"""

    thread_id: UUID = Field(..., description="Thread to save")
    note_title: str = Field(
        ..., min_length=1, max_length=255, description="Title for the new note"
    )
    include_citations: bool = Field(
        default=True, description="Include citations in the note"
    )
