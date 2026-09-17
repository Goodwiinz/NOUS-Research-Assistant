"""
Export schemas for thread/conversation export functionality.

Supports exporting threads in multiple formats:
- Markdown: Human-readable with citations
- PDF: Formatted document with styling
- JSON: Complete data for re-import or analysis
- HTML: Self-contained viewable file
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ExportFormat(str, Enum):
    """Supported export formats."""

    MARKDOWN = "markdown"
    PDF = "pdf"
    JSON = "json"
    HTML = "html"


class ExportOptions(BaseModel):
    """Options for customizing export output."""

    include_system_messages: bool = Field(
        default=False, description="Include system messages in export"
    )
    include_citations: bool = Field(
        default=True, description="Include citation references and snippets"
    )
    include_attachments: bool = Field(
        default=True, description="Include attachment metadata"
    )
    include_metadata: bool = Field(
        default=True, description="Include message metadata (timestamps, model info)"
    )
    include_feedback: bool = Field(
        default=False, description="Include user feedback ratings"
    )
    date_format: str = Field(
        default="%Y-%m-%d %H:%M:%S", description="Date format string for timestamps"
    )


class ExportRequest(BaseModel):
    """Request schema for single thread export."""

    thread_id: str = Field(..., description="Thread ID to export")
    format: ExportFormat = Field(
        default=ExportFormat.MARKDOWN, description="Export format"
    )
    options: ExportOptions = Field(
        default_factory=ExportOptions, description="Export customization options"
    )


class BatchExportRequest(BaseModel):
    """Request schema for batch thread export."""

    thread_ids: List[str] = Field(
        ..., min_length=1, max_length=100, description="List of thread IDs to export"
    )
    format: ExportFormat = Field(
        default=ExportFormat.MARKDOWN, description="Export format for all threads"
    )
    options: ExportOptions = Field(
        default_factory=ExportOptions, description="Export customization options"
    )
    as_zip: bool = Field(
        default=True, description="Package multiple exports as ZIP file"
    )


class CitationExport(BaseModel):
    """Exported citation data."""

    id: str
    document_id: Optional[str] = None
    external_reference_id: Optional[str] = None
    document_title: Optional[str] = None
    document_type: Optional[str] = None
    snippet: Optional[str] = None
    page_number: Optional[int] = None
    score: Optional[float] = None


class ProviderTokenUsage(BaseModel):
    """Provider-reported token usage for one persisted model response."""

    input_tokens: Optional[int] = Field(
        default=None, ge=0, description="Provider-reported input/prompt tokens"
    )
    output_tokens: Optional[int] = Field(
        default=None, ge=0, description="Provider-reported output/completion tokens"
    )


class MessageExport(BaseModel):
    """Exported message data."""

    id: str
    role: str
    content: str
    created_at: datetime
    model_name: Optional[str] = None
    provider_usage: Optional[ProviderTokenUsage] = Field(
        default=None,
        description=(
            "Provider-reported usage persisted for this message; missing members "
            "remain unknown"
        ),
    )
    token_count: int = 0
    latency_ms: Optional[int] = None
    feedback_rating: Optional[int] = None
    feedback_text: Optional[str] = None
    citations: List[CitationExport] = Field(default_factory=list)
    has_attachments: bool = False


class ThreadExport(BaseModel):
    """Complete exported thread data."""

    id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime
    message_count: int
    token_count: int
    conversation_id: str
    messages: List[MessageExport] = Field(default_factory=list)

    # Metadata
    export_format: ExportFormat
    exported_at: datetime = Field(default_factory=datetime.utcnow)
    export_version: str = "1.0"


class ExportResponse(BaseModel):
    """Response schema for export endpoints."""

    success: bool
    format: ExportFormat
    filename: str
    content_type: str
    size_bytes: int
    thread_count: int = 1
    message_count: int = 0
    citation_count: int = 0


class ExportError(BaseModel):
    """Error response for export failures."""

    error: str
    thread_id: Optional[str] = None
    detail: Optional[str] = None
