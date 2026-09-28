"""
Shared enumerations for API validation

These enums provide type-safe validation for query parameters
to prevent SQL injection and other security vulnerabilities.
"""

from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only import to avoid a cycle
    from src.models.document import ProcessingStatus


class JobStatus(StrEnum):
    """Agent job lifecycle status — the wire contract for ``GET /agent/jobs/{id}``.

    Single source of truth for every job-store status write (audit finding C7:
    the backend previously wrote six untyped strings while the frontend typed
    four). Members ARE the wire strings (``StrEnum``), so they JSON-serialize
    and compare against raw Redis records transparently.

    The historical ``failed``/``error`` split is collapsed: writers always
    write ``FAILED``; ``"error"`` is accepted as an inbound alias when reading
    records written by pre-collapse code (one-release transition, see
    ``_missing_``).

    ``QUEUED`` (run created, worker not started) and ``STOPPING`` (cancel
    requested, not yet acknowledged) belong to the durable run lifecycle:
    queued → running ⇄ awaiting_confirmation → completed|failed|cancelled,
    with stopping as a visible transient before cancelled. Both are
    non-terminal; terminal states are absorbing.
    """

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    STOPPING = "stopping"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def _missing_(cls, value: object) -> "JobStatus | None":
        # Inbound alias for the one-release failed/error transition: records
        # written before the collapse may still hold "error" in Redis (TTL 1h)
        # or arrive from a not-yet-redeployed writer. Never written back.
        if isinstance(value, str) and value.lower() == "error":
            return cls.FAILED
        return None

    @property
    def is_terminal(self) -> bool:
        """True when the job can never transition again (stop polling)."""
        return self in TERMINAL_JOB_STATUSES


TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


class AgentOutboxStatus(StrEnum):
    """Lifecycle of a transactional-outbox dispatch record (``agent_outbox``).

    ``PENDING`` is written inside the accept transaction — it means "the system
    has durably promised to dispatch this run", not "dispatch has happened".
    ``DISPATCHED`` is stamped once the dispatch actually occurred (today: the
    in-process graph iterator opened on the ``/stream`` path). ``FAILED`` marks
    a record a future relay gave up on; nothing writes it yet.

    Members ARE the stored strings (``StrEnum``), and the set is rendered into
    the table's CHECK constraint, so the column domain cannot drift from this
    enum without a migration.
    """

    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class DocumentSortField(str, Enum):
    """
    Allowed sort fields for document queries.

    SECURITY: Only these fields can be used for sorting documents.
    Using an enum prevents SQL injection via arbitrary column names.
    """

    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    TITLE = "title"
    FILENAME = "filename"
    FILE_SIZE = "file_size_bytes"
    PROCESSING_STATUS = "processing_status"
    DOCUMENT_TYPE = "document_type"


class DocumentDateRange(str, Enum):
    """
    Allowed quick date-range filters for document search.

    SECURITY: An unconstrained free-text ``date_range`` query param is
    silently ignored by every branch that doesn't match, which looks like a
    working filter to the caller but returns unfiltered results. Restricting
    it to an enum makes an unknown value a 422 instead of a silent no-op.
    """

    LAST_WEEK = "last_week"
    LAST_MONTH = "last_month"
    LAST_YEAR = "last_year"


class SortOrder(str, Enum):
    """
    Sort order direction.

    SECURITY: Restricts sort order to valid SQL directions only.
    """

    ASC = "asc"
    DESC = "desc"


class EntitySortField(str, Enum):
    """
    Allowed sort fields for entity queries.
    """

    CREATED_AT = "created_at"
    NAME = "name"
    ENTITY_TYPE = "entity_type"
    CONFIDENCE_SCORE = "confidence_score"


class JobSortField(str, Enum):
    """
    Allowed sort fields for processing job queries.
    """

    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    STATUS = "status"
    PRIORITY = "priority"
    STARTED_AT = "started_at"
    COMPLETED_AT = "completed_at"


class SearchSortField(str, Enum):
    """
    Allowed sort fields for search results.
    """

    RELEVANCE = "relevance"
    DATE = "date"
    TITLE = "title"


class UserSortField(str, Enum):
    """
    Allowed sort fields for user queries.
    """

    CREATED_AT = "created_at"
    EMAIL = "email"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    LAST_LOGIN = "last_login_at"


class OrganizationSortField(str, Enum):
    """
    Allowed sort fields for organization queries.
    """

    CREATED_AT = "created_at"
    NAME = "name"
    MEMBER_COUNT = "member_count"


# Model to field mapping for validation
SORT_FIELD_MAPPINGS = {
    "Document": {field.value for field in DocumentSortField},
    "Entity": {field.value for field in EntitySortField},
    "ProcessingJob": {field.value for field in JobSortField},
    "User": {field.value for field in UserSortField},
    "Organization": {field.value for field in OrganizationSortField},
}


def validate_sort_field(model_name: str, field_name: str) -> bool:
    """
    Validate if a sort field is allowed for a given model.

    Args:
        model_name: Name of the SQLAlchemy model
        field_name: Field name to validate

    Returns:
        True if the field is allowed, False otherwise
    """
    allowed_fields = SORT_FIELD_MAPPINGS.get(model_name, set())
    return field_name in allowed_fields


class ApiDocumentStatus(StrEnum):
    """Public, client-facing document processing status vocabulary.

    The database stores raw ``ProcessingStatus`` values
    (``pending``/``processing``/``completed``/``failed``/``retrying``). The API
    exposes a stable, narrower vocabulary to clients. These classmethods are the
    single source of truth for translating between the two in both directions,
    replacing the per-router inline dicts that had drifted apart and caused two
    production incidents (a filter 500 and a false ``queued`` state).

    Direction quirks (encoded below, do not "simplify"):
      * db ``pending``   -> api ``queued``
      * db ``completed`` -> api ``indexed``
      * db ``retrying``  -> api ``processing`` (collapsed; never surfaced raw)
    So the mapping is *not* a clean bijection: ``retrying`` folds into
    ``processing`` on the way out. ``to_db`` therefore maps ``processing`` back
    to ``PROCESSING`` (not ``RETRYING``) and additionally accepts the raw db
    spellings (``pending``/``completed``/``retrying``) as filter input for
    backwards compatibility.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"

    @classmethod
    def from_db(cls, db_status: object) -> "ApiDocumentStatus":
        """Map a stored ``ProcessingStatus`` (enum, ``.value`` str, or ``None``)
        to the public API status. Unknown/None values fall back to ``QUEUED``
        (preserving the historical ``to_dict`` default)."""
        raw = getattr(db_status, "value", db_status)
        key = str(raw).lower() if raw is not None else ""
        return {
            "pending": cls.QUEUED,
            "processing": cls.PROCESSING,
            "completed": cls.INDEXED,
            "failed": cls.FAILED,
            "retrying": cls.PROCESSING,
        }.get(key, cls.QUEUED)

    @classmethod
    def to_db(cls, api_status: object) -> "ProcessingStatus":
        """Map a client-supplied status filter to the backend ``ProcessingStatus``
        enum member. Accepts the public vocabulary
        (``queued``/``processing``/``indexed``/``failed``) plus the raw db
        spellings (``pending``/``completed``/``retrying``).

        Raises:
            ValueError: if the value is not a recognised status. Callers wire
                this to an HTTP 400 (keeps the validated-enum injection-prevention
                pattern — only known values ever reach the query).
        """
        from src.models.document import ProcessingStatus

        raw = getattr(api_status, "value", api_status)
        key = str(raw).lower() if raw is not None else ""
        mapping = {
            # public API vocabulary
            "queued": ProcessingStatus.PENDING,
            "processing": ProcessingStatus.PROCESSING,
            "indexed": ProcessingStatus.COMPLETED,
            "failed": ProcessingStatus.FAILED,
            # raw backend spellings, accepted for convenience/back-compat
            "pending": ProcessingStatus.PENDING,
            "completed": ProcessingStatus.COMPLETED,
            "retrying": ProcessingStatus.RETRYING,
        }
        try:
            return mapping[key]
        except KeyError as exc:
            raise ValueError(f"Invalid processing_status: {api_status}") from exc

    @classmethod
    def try_to_db(cls, api_status: object) -> Optional["ProcessingStatus"]:
        """Like :meth:`to_db` but returns ``None`` instead of raising for
        unknown values."""
        try:
            return cls.to_db(api_status)
        except ValueError:
            return None


class AgentStreamEvent(StrEnum):
    """Wire vocabulary for agent SSE frames — the single source of truth for
    every ``event:`` name the agent stream emits (audit finding C1).

    The backend previously spelled these 12 names as inline string literals at
    ~40 ``emitter.emit(...)`` sites, the ``/stream`` docstring listed only 6,
    the resume path hand-listed a terminal subset, and the frontend consumer
    switch mirrored the list a fourth time — four copies that drifted. Members
    ARE the wire strings (``StrEnum``), so ``emitter.emit(AgentStreamEvent.TOKEN,
    ...)`` serializes byte-identically to the old literal ``"token"`` (zero wire
    change). The frontend mirror lives in
    ``frontend/src/services/agentStreamEvents.ts``; a contract test on each side
    fails CI if a new emit literal (or switch case) drifts from this enum.

    Values are the frozen wire contract — do NOT rename them.
    """

    TOKEN = "token"
    REASONING_DELTA = "reasoning_delta"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    RAG_CONTEXT = "rag_context"
    PLAN = "plan"
    REFLECTION = "reflection"
    TRACE = "trace"
    USAGE = "usage"
    # Keepalive with no payload; the frontend intentionally drops it (see
    # agentStreamEvents.ts). Every OTHER member must be handled by the consumer.
    HEARTBEAT = "heartbeat"
    STATUS = "status"
    CONFIRMATION = "confirmation"
    APPROVAL_REQUIRED = "approval_required"
    DONE = "done"
    ERROR = "error"


class AgentErrorCategory(StrEnum):
    """Wire vocabulary for the ``category`` key on agent SSE ``error`` frames —
    and, deliberately, the future RFC 9457 problem-type vocabulary for the HTTP
    error envelope. One source, two transports.

    An ``error`` frame's ``error`` key stays a human-readable **string** (the
    client-safe message); ``category`` is a sibling top-level key carrying this
    machine-readable label, so a consumer can branch on *why* a turn failed
    without regex-matching prose. The frame stays flat and ``schema_version``
    stays ``"1.0"``: adding a sibling key is backward compatible, turning
    ``error`` into an object would not be.

    Categories are authored by the **server** at the emit site (see
    ``src.services.agent._errors.classify_agent_error`` /
    ``error_frame_payload``) — the frontend mirror in
    ``frontend/src/services/agentStreamEvents.ts`` only validates against this
    list and ignores anything it does not know.

    Values are the frozen wire contract — do NOT rename them.
    """

    #: An upstream call (model, tool, checkpointer) exceeded its deadline.
    UPSTREAM_TIMEOUT = "upstream_timeout"
    #: The LLM provider returned an error (bad status, API failure).
    MODEL_ERROR = "model_error"
    #: A tool invocation failed in a way that ended the turn.
    TOOL_ERROR = "tool_error"
    #: LangGraph checkpoint could not be read/written — resume is impossible.
    CHECKPOINT_UNAVAILABLE = "checkpoint_unavailable"
    #: Provider (or our own gate) rate-limited the request; retry later.
    RATE_LIMITED = "rate_limited"
    #: The run was cancelled (client disconnect / user stop).
    CANCELLED = "cancelled"
    #: The request itself is malformed or unresolvable; retrying as-is fails.
    INVALID_REQUEST = "invalid_request"
    #: Conflicting concurrent operation (e.g. a confirmation already claimed).
    CONFLICT = "conflict"
    #: Anything else — an unclassified server-side fault.
    INTERNAL = "internal"


# Terminal frames: after one of these the live stream ends and the resumable
# buffer stops replaying (see ``resume_stream`` in api/agent/execute.py). A
# ``confirmation`` frame is terminal for the stream even though the turn later
# resumes via /stream/confirm — the socket that saw it is done.
TERMINAL_STREAM_EVENTS: frozenset[AgentStreamEvent] = frozenset(
    {
        AgentStreamEvent.DONE,
        AgentStreamEvent.ERROR,
        AgentStreamEvent.CONFIRMATION,
    }
)


class SatelliteSyncStatus(StrEnum):
    """Per-satellite fan-out outcome recorded on ``documents`` (audit D1).

    Ingestion fans a document out to satellite indexes (Neo4j knowledge graph,
    DO Knowledge Base) on a best-effort basis: a satellite failure is
    deliberately non-fatal and the document still reaches COMPLETED. These
    values record the truth of each satellite attempt
    (``documents.neo4j_index_status`` / ``documents.do_kb_sync_status``) so a
    drifted document is distinguishable from a healthy one and the scheduled
    reconciler (``src.tasks.reconcile_tasks``) can re-drive failures.

    ``NULL`` (column left unset) means the satellite write was never attempted
    — e.g. the document has no extracted text, or DO KB is disabled.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
