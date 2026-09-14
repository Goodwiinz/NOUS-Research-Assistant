"""
Export service for thread/conversation export functionality.

Handles conversion of thread data to various formats:
- Markdown: Human-readable with citations
- PDF: Formatted document (requires weasyprint)
- JSON: Complete data for re-import or analysis
- HTML: Self-contained viewable file
"""

import io
import json
import re
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, BinaryIO, Dict, List, Optional, Protocol
from urllib.parse import SplitResult, parse_qsl, quote, urlsplit, urlunsplit

import structlog
from jinja2 import BaseLoader, Environment, select_autoescape
from jinja2.ext import loopcontrols
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.models.chat_message import ChatMessage, MessageRole
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.thread import Thread
from src.shared.export_schemas import (
    CitationExport,
    ExportFormat,
    ExportOptions,
    MessageExport,
    ThreadExport,
)

logger = structlog.get_logger(__name__)

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_[\]{}()<>])")
_ARXIV_ID = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_MARKDOWN_URL_SAFE = ":/?#[]@!$&'*+,;=%~-._"
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "exp",
        "expires",
        "expiry",
        "id_token",
        "refresh_token",
        "sig",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-expires",
        "x-amz-security-token",
        "x-amz-signature",
        "x-goog-credential",
        "x-goog-expires",
        "x-goog-signature",
    }
)


def _split_safe_http_url(value: str) -> Optional[SplitResult]:
    """Parse a link destination, rejecting active schemes and credentials."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None

    try:
        parsed = urlsplit(value.strip())
        # Reading ``port`` also rejects malformed/out-of-range port syntax.
        parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if any(char.isspace() for char in parsed.netloc) or "\\" in parsed.netloc:
        return None
    return parsed


def _safe_markdown_destination(value: str) -> Optional[str]:
    """Return a Markdown-safe absolute HTTP(S) URL, or no destination."""
    parsed = _split_safe_http_url(value)
    if (
        parsed is None
        or _has_sensitive_query(parsed.query)
        or _has_sensitive_query(parsed.fragment)
    ):
        return None
    normalized = urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return quote(normalized, safe=_MARKDOWN_URL_SAFE)


def _configured_frontend_origin() -> Optional[str]:
    """Resolve the trusted frontend origin without consulting request headers."""
    base_url = settings.FRONTEND_BASE_URL.strip()
    if not base_url:
        origins = settings.cors_origins_list
        base_url = origins[0] if origins else ""

    parsed = _split_safe_http_url(base_url)
    if parsed is None:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", "")).rstrip("/")


def _external_reference_destination(reference: Optional[str]) -> Optional[str]:
    """Resolve only external identifier formats with canonical public URLs."""
    if not reference:
        return None

    direct_url = _safe_markdown_destination(reference)
    if direct_url:
        return direct_url

    normalized = reference.strip()
    arxiv_id = (
        normalized[6:].strip()
        if normalized.lower().startswith("arxiv:")
        else normalized
    )
    if _ARXIV_ID.fullmatch(arxiv_id):
        return f"https://arxiv.org/abs/{quote(arxiv_id, safe='/.')}"

    doi = (
        normalized[4:].strip() if normalized.lower().startswith("doi:") else normalized
    )
    if _DOI.fullmatch(doi):
        return f"https://doi.org/{quote(doi, safe='/.-_:;')}"

    return None


def _has_sensitive_query(query: str) -> bool:
    """Detect common signed/expiring URL credentials without logging values."""
    query_keys = {key.casefold() for key, _ in parse_qsl(query, keep_blank_values=True)}
    return not _SENSITIVE_QUERY_KEYS.isdisjoint(query_keys)


def _contains_sensitive_url_data(value: Optional[str]) -> bool:
    """Return whether an identifier embeds credentials or signed query data."""
    if not value:
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return (
        parsed.username is not None
        or parsed.password is not None
        or _has_sensitive_query(parsed.query)
        or _has_sensitive_query(parsed.fragment)
    )


def _escape_markdown_label(value: str) -> str:
    """Escape source labels without allowing line or inline-markup injection."""
    flattened = " ".join(value.splitlines())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", flattened)


# Markdown template
MARKDOWN_TEMPLATE = """# {{ thread.title or "Untitled Thread" }}

{% if thread.summary %}
> {{ thread.summary }}
{% endif %}

**Status**: {{ thread.status }}
**Created**: {{ thread.created_at.strftime(date_format) }}
**Messages**: {{ thread.message_count }}
{% if options.include_metadata %}
**Tokens**: {{ thread.token_count }}
{% endif %}

---

{% for message in thread.messages %}
{% if message.role == "system" and not options.include_system_messages %}
{% continue %}
{% endif %}
## {{ message.role | title }}
*{{ message.created_at.strftime(date_format) }}*{% if message.model_name and options.include_metadata %} | Model: {{ message.model_name }}{% endif %}

{{ message.content }}

{% if message.citations and options.include_citations %}
### Sources
{% for citation in message.citations %}
- {{ citation | citation_source(loop.index) }}{% if citation.page_number is not none %} (p. {{ citation.page_number }}){% endif %}{% if citation.score %} [{{ "%.2f"|format(citation.score) }}]{% endif %}
{% if citation.snippet %}
  > {{ citation.snippet | truncate(200) }}
{% endif %}
{% endfor %}
{% endif %}

{% if message.feedback_rating and options.include_feedback %}
**Feedback**: {{ "⭐" * message.feedback_rating }}{% if message.feedback_text %} - {{ message.feedback_text }}{% endif %}
{% endif %}

---

{% endfor %}

---
*Exported on {{ exported_at.strftime(date_format) }}*
"""

# HTML template
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ thread.title or "Thread Export" }}</title>
    <style>
        :root {
            --phosphor-green: #00ff9f;
            --amber: #ffb700;
            --cyan: #00d4ff;
            --bg-dark: #0a0a0a;
            --bg-card: #141414;
            --text-primary: #e4e4e7;
            --text-secondary: #a1a1aa;
        }
        body {
            font-family: 'JetBrains Mono', 'SF Mono', Monaco, monospace;
            background: var(--bg-dark);
            color: var(--text-primary);
            max-width: 800px;
            margin: 0 auto;
            padding: 2rem;
            line-height: 1.6;
        }
        h1 { color: var(--phosphor-green); border-bottom: 2px solid var(--phosphor-green); padding-bottom: 0.5rem; }
        h2 { color: var(--cyan); margin-top: 2rem; }
        .metadata { color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 2rem; }
        .message { background: var(--bg-card); padding: 1.5rem; margin: 1rem 0; border-radius: 8px; border-left: 3px solid var(--cyan); }
        .message.user { border-left-color: var(--phosphor-green); }
        .message.assistant { border-left-color: var(--cyan); }
        .message.system { border-left-color: var(--amber); opacity: 0.8; }
        .message-header { display: flex; justify-content: space-between; margin-bottom: 1rem; color: var(--text-secondary); font-size: 0.85rem; }
        .role { font-weight: bold; text-transform: uppercase; }
        .role.user { color: var(--phosphor-green); }
        .role.assistant { color: var(--cyan); }
        .role.system { color: var(--amber); }
        .content { white-space: pre-wrap; word-wrap: break-word; }
        .citations { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #333; }
        .citation { background: #1a1a1a; padding: 0.75rem; margin: 0.5rem 0; border-radius: 4px; font-size: 0.85rem; }
        .citation-title { color: var(--amber); font-weight: bold; }
        .citation-snippet { color: var(--text-secondary); margin-top: 0.5rem; font-style: italic; }
        .footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #333; color: var(--text-secondary); font-size: 0.8rem; text-align: center; }
        blockquote { border-left: 3px solid var(--amber); padding-left: 1rem; margin: 1rem 0; color: var(--text-secondary); }
    </style>
</head>
<body>
    <h1>{{ thread.title or "Untitled Thread" }}</h1>
    
    {% if thread.summary %}
    <blockquote>{{ thread.summary }}</blockquote>
    {% endif %}
    
    <div class="metadata">
        <strong>Status:</strong> {{ thread.status }} | 
        <strong>Created:</strong> {{ thread.created_at.strftime(date_format) }} | 
        <strong>Messages:</strong> {{ thread.message_count }}
        {% if options.include_metadata %} | <strong>Tokens:</strong> {{ thread.token_count }}{% endif %}
    </div>
    
    {% for message in thread.messages %}
    {% if message.role == "system" and not options.include_system_messages %}
    {% continue %}
    {% endif %}
    <div class="message {{ message.role }}">
        <div class="message-header">
            <span class="role {{ message.role }}">{{ message.role }}</span>
            <span>{{ message.created_at.strftime(date_format) }}{% if message.model_name and options.include_metadata %} | {{ message.model_name }}{% endif %}</span>
        </div>
        <div class="content">{{ message.content }}</div>
        
        {% if message.citations and options.include_citations %}
        <div class="citations">
            <strong>Sources:</strong>
            {% for citation in message.citations %}
            <div class="citation">
                <span class="citation-title">{{ citation.document_title or citation.external_reference_id or "Source " ~ loop.index }}</span>
                {% if citation.page_number %} (p. {{ citation.page_number }}){% endif %}
                {% if citation.score %} [{{ "%.2f"|format(citation.score) }}]{% endif %}
                {% if citation.snippet %}
                <div class="citation-snippet">{{ citation.snippet | truncate(200) }}</div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>
    {% endfor %}
    
    <div class="footer">
        Exported on {{ exported_at.strftime(date_format) }} | RAG System Thread Export v1.0
    </div>
</body>
</html>
"""


class ExportFormatter(ABC):
    """Abstract base class for export formatters."""

    @abstractmethod
    def format(self, thread: ThreadExport, options: ExportOptions) -> bytes:
        """Format thread data to bytes."""
        pass

    @property
    @abstractmethod
    def content_type(self) -> str:
        """MIME content type."""
        pass

    @property
    @abstractmethod
    def file_extension(self) -> str:
        """File extension without dot."""
        pass


class MarkdownFormatter(ExportFormatter):
    """Format thread as Markdown."""

    def __init__(self):
        self._frontend_origin = _configured_frontend_origin()
        # Enable autoescape for security (even for Markdown)
        self._env = Environment(
            loader=BaseLoader(), extensions=[loopcontrols], autoescape=True
        )
        self._env.filters["citation_source"] = self._citation_source
        self._template = self._env.from_string(MARKDOWN_TEMPLATE)

    def _citation_source(self, citation: CitationExport, source_number: int) -> Markup:
        """Render one bold source label, linking only to stable safe targets."""
        reference = citation.external_reference_id
        if citation.document_title:
            label = citation.document_title
        elif _contains_sensitive_url_data(reference):
            # Never copy URL credentials or signed query secrets into an export.
            label = f"Source {source_number}"
        else:
            label = reference or f"Source {source_number}"

        destination = None
        if citation.document_id and self._frontend_origin:
            document_id = quote(citation.document_id, safe="")
            destination = f"{self._frontend_origin}/documents/{document_id}"
        elif reference:
            destination = _external_reference_destination(reference)

        escaped_label = _escape_markdown_label(label)
        if destination:
            return Markup(f"**[{escaped_label}]({destination})**")
        return Markup(f"**{escaped_label}**")

    def format(self, thread: ThreadExport, options: ExportOptions) -> bytes:
        content = self._template.render(
            thread=thread,
            options=options,
            date_format=options.date_format,
            exported_at=datetime.utcnow(),
        )
        return content.encode("utf-8")

    @property
    def content_type(self) -> str:
        return "text/markdown; charset=utf-8"

    @property
    def file_extension(self) -> str:
        return "md"


class HTMLFormatter(ExportFormatter):
    """Format thread as HTML."""

    def __init__(self):
        # Enable autoescape for HTML/XML to prevent XSS attacks
        self._env = Environment(
            loader=BaseLoader(),
            extensions=[loopcontrols],
            autoescape=select_autoescape(["html", "xml"]),
        )
        self._template = self._env.from_string(HTML_TEMPLATE)

    def format(self, thread: ThreadExport, options: ExportOptions) -> bytes:
        content = self._template.render(
            thread=thread,
            options=options,
            date_format=options.date_format,
            exported_at=datetime.utcnow(),
        )
        return content.encode("utf-8")

    @property
    def content_type(self) -> str:
        return "text/html; charset=utf-8"

    @property
    def file_extension(self) -> str:
        return "html"


class JSONFormatter(ExportFormatter):
    """Format thread as JSON."""

    def format(self, thread: ThreadExport, options: ExportOptions) -> bytes:
        data = thread.model_dump(mode="json")
        data["export_options"] = options.model_dump()
        content = json.dumps(data, indent=2, default=str)
        return content.encode("utf-8")

    @property
    def content_type(self) -> str:
        return "application/json; charset=utf-8"

    @property
    def file_extension(self) -> str:
        return "json"


class PDFFormatter(ExportFormatter):
    """Format thread as PDF using WeasyPrint."""

    def __init__(self):
        self._html_formatter = HTMLFormatter()
        self._weasyprint_available = self._check_weasyprint()

    def _check_weasyprint(self) -> bool:
        try:
            import weasyprint

            return True
        except ImportError:
            logger.warning(
                "WeasyPrint not installed, PDF export will use HTML fallback"
            )
            return False

    def format(self, thread: ThreadExport, options: ExportOptions) -> bytes:
        html_content = self._html_formatter.format(thread, options)

        if not self._weasyprint_available:
            # Fallback: return HTML with PDF content type suggestion
            logger.warning(
                "PDF export falling back to HTML - install weasyprint for true PDF"
            )
            return html_content

        try:
            import weasyprint

            html_doc = weasyprint.HTML(string=html_content.decode("utf-8"))
            pdf_bytes = html_doc.write_pdf()
            return pdf_bytes
        except Exception as e:
            logger.error("PDF generation failed", error=str(e))
            raise RuntimeError(f"PDF generation failed: {e}")

    @property
    def content_type(self) -> str:
        if self._weasyprint_available:
            return "application/pdf"
        return "text/html; charset=utf-8"

    @property
    def file_extension(self) -> str:
        if self._weasyprint_available:
            return "pdf"
        return "html"


class ExportService:
    """Service for exporting threads to various formats."""

    def __init__(self, db: AsyncSession):
        self._db = db
        self._formatters: Dict[ExportFormat, ExportFormatter] = {
            ExportFormat.MARKDOWN: MarkdownFormatter(),
            ExportFormat.HTML: HTMLFormatter(),
            ExportFormat.JSON: JSONFormatter(),
            ExportFormat.PDF: PDFFormatter(),
        }

    async def export_thread(
        self, thread_id: str, user_id: str, format: ExportFormat, options: ExportOptions
    ) -> tuple[bytes, str, str]:
        """
        Export a single thread.

        Returns:
            tuple: (content_bytes, filename, content_type)
        """
        thread = await self._load_thread(thread_id, user_id, options)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found or access denied")

        formatter = self._formatters[format]
        content = formatter.format(thread, options)

        # Generate filename
        title_slug = self._slugify(thread.title or "thread")
        filename = f"{title_slug}_{thread_id[:8]}.{formatter.file_extension}"

        logger.info(
            "Thread exported",
            thread_id=thread_id,
            format=format.value,
            size_bytes=len(content),
        )

        return content, filename, formatter.content_type

    async def export_batch(
        self,
        thread_ids: List[str],
        user_id: str,
        format: ExportFormat,
        options: ExportOptions,
        as_zip: bool = True,
    ) -> tuple[bytes, str, str]:
        """
        Export multiple threads.

        Returns:
            tuple: (content_bytes, filename, content_type)
        """
        if not as_zip and len(thread_ids) > 1:
            raise ValueError("Multiple threads require ZIP packaging")

        if len(thread_ids) == 1 and not as_zip:
            return await self.export_thread(thread_ids[0], user_id, format, options)

        # Create ZIP archive
        zip_buffer = io.BytesIO()
        formatter = self._formatters[format]

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for thread_id in thread_ids:
                try:
                    thread = await self._load_thread(thread_id, user_id, options)
                    if not thread:
                        logger.warning(
                            "Thread not found, skipping", thread_id=thread_id
                        )
                        continue

                    content = formatter.format(thread, options)
                    title_slug = self._slugify(thread.title or "thread")
                    filename = (
                        f"{title_slug}_{thread_id[:8]}.{formatter.file_extension}"
                    )

                    zf.writestr(filename, content)

                except Exception as e:
                    logger.error(
                        "Failed to export thread", thread_id=thread_id, error=str(e)
                    )
                    # Add error file
                    zf.writestr(f"error_{thread_id[:8]}.txt", f"Export failed: {e}")

        zip_buffer.seek(0)
        zip_content = zip_buffer.read()

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"thread_export_{timestamp}.zip"

        logger.info(
            "Batch export completed",
            thread_count=len(thread_ids),
            format=format.value,
            size_bytes=len(zip_content),
        )

        return zip_content, filename, "application/zip"

    async def _load_thread(
        self, thread_id: str, user_id: str, options: ExportOptions
    ) -> Optional[ThreadExport]:
        """Load thread with messages and citations."""
        query = (
            select(Thread).options(
                selectinload(Thread.messages).selectinload(ChatMessage.citations),
                # has_attachments (chat_message.py) touches the attachments
                # relationship; without eager-loading it, the export path
                # sync-lazy-loads inside the async session and dies with
                # MissingGreenlet ("greenlet_spawn has not been called") —
                # every thread export 500'd (Sentry JAVASCRIPT-NEXTJS-4Q,
                # 2026-08-12).
                selectinload(Thread.messages).selectinload(ChatMessage.attachments),
                # Load conversation (+ its workspace, for the soft-delete
                # cascade check below) for ownership + soft-delete checks.
                selectinload(Thread.conversation).selectinload(Conversation.workspace),
            )
            # R5-M12: a soft-deleted thread must not be exportable. Neither
            # delete cascades to children (see workspace_access.py), so the
            # thread's own is_deleted is filtered here and the parent
            # conversation/workspace are re-checked explicitly below.
            .where(Thread.id == thread_id, Thread.is_deleted == False)  # noqa: E712
        )

        result = await self._db.execute(query)
        thread = result.unique().scalar_one_or_none()

        if not thread:
            return None

        # R5-M12: a soft-deleted parent conversation or workspace revokes
        # export access to the thread even though its own is_deleted stayed
        # False (same convention as workspace_access.get_thread).
        if thread.conversation.is_deleted or thread.conversation.workspace.is_deleted:
            return None

        # Authorization check: Verify user owns the thread or has admin privileges
        # Thread ownership is determined by:
        # 1. User created the thread directly (thread.created_by_id == user_id)
        # 2. User owns the parent conversation (conversation.created_by_id == user_id)
        if (
            str(thread.created_by_id) != user_id
            and str(thread.conversation.created_by_id) != user_id
        ):
            logger.warning(
                "Unauthorized thread export attempt",
                thread_id=thread_id,
                user_id=user_id,
                thread_owner=str(thread.created_by_id),
                conversation_owner=str(thread.conversation.created_by_id),
            )
            return None

        # Convert to export schema
        messages = []
        for msg in thread.messages:
            # R5-M12: a soft-deleted message (which may carry PII the user
            # believes scrubbed/removed) must never reappear in an export.
            if msg.is_deleted:
                continue

            # Edit-and-resend tombstone: an exported document must not contain
            # a turn the user replaced (nor its answer).
            if msg.superseded_by_message_id is not None:
                continue

            # Skip system messages if not requested
            if msg.role == MessageRole.SYSTEM and not options.include_system_messages:
                continue

            citations = []
            if options.include_citations and msg.citations:
                for cit in msg.citations:
                    citations.append(
                        CitationExport(
                            id=str(cit.id),
                            document_id=(
                                str(cit.document_id) if cit.document_id else None
                            ),
                            external_reference_id=cit.external_reference_id,
                            document_title=cit.document_title,
                            document_type=cit.document_type,
                            snippet=cit.snippet,
                            page_number=cit.page_number,
                            score=cit.score,
                        )
                    )

            messages.append(
                MessageExport(
                    id=str(msg.id),
                    role=msg.role.value,
                    content=msg.content,
                    created_at=msg.created_at,
                    model_name=msg.model_name if options.include_metadata else None,
                    token_count=msg.token_count if options.include_metadata else 0,
                    latency_ms=msg.latency_ms if options.include_metadata else None,
                    feedback_rating=(
                        msg.feedback_rating if options.include_feedback else None
                    ),
                    feedback_text=(
                        msg.feedback_text if options.include_feedback else None
                    ),
                    citations=citations,
                    has_attachments=(
                        msg.has_attachments if options.include_attachments else False
                    ),
                )
            )

        return ThreadExport(
            id=str(thread.id),
            title=thread.title,
            summary=thread.summary,
            status=thread.status.value if thread.status else "unknown",
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            last_message_at=thread.last_message_at,
            message_count=thread.message_count,
            token_count=thread.token_count,
            conversation_id=str(thread.conversation_id),
            messages=messages,
            export_format=ExportFormat.MARKDOWN,  # Will be overwritten by formatter
        )

    def _slugify(self, text: str, max_length: int = 50) -> str:
        """Convert text to URL-safe slug."""
        text = text.lower()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[-\s]+", "_", text)
        return text[:max_length].strip("_")


def get_export_service(db: AsyncSession) -> ExportService:
    """Factory function for ExportService."""
    return ExportService(db)
