"""
ChatMessage model for Terminal Observatory thread-centric chat schema
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class MessageRole(PyEnum):
    """Message role types"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """
    ChatMessage model - individual messages within a thread.

    Represents a single message in a conversation thread, supporting
    user messages, AI responses, system messages, and tool outputs.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        # Partial unique index backing the ON CONFLICT idempotency upsert in
        # jobs._persist_user_message. Declared on the model so create_all
        # (local docker-compose bootstrap) produces it too — on deployed
        # clusters it already exists via migrations v0a1b2c3d4e5 /
        # ensure_upsert_uq_constraints; the predicate must stay byte-identical
        # to those migrations or ON CONFLICT inference breaks.
        Index(
            "uq_chat_messages_thread_client_msg_user",
            "thread_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL AND role = 'user'"),
        ),
        # Assistant-role mirror (migration y7z8a9b0c1d2) backing the
        # idempotent assistant insert in jobs._persist_assistant_message —
        # same byte-identical-predicate rule as above.
        Index(
            "uq_chat_messages_thread_client_msg_assistant",
            "thread_id",
            "client_message_id",
            unique=True,
            postgresql_where=text(
                "client_message_id IS NOT NULL AND role = 'assistant'"
            ),
        ),
        # Tombstone lookup (migration e6f7a8b9c0d1). Partial because superseded
        # rows are the rare case: display/model/export reads all filter
        # ``superseded_by_message_id IS NULL`` on top of the existing
        # (thread_id, created_at) indexes, and only the audit/repair direction
        # ("which rows did this edit tombstone?") needs an index of its own.
        Index(
            "ix_chat_messages_superseded",
            "thread_id",
            postgresql_where=text("superseded_by_message_id IS NOT NULL"),
        ),
    )

    # Parent relationship
    thread_id = Column(
        GUID(), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Sender
    user_id = Column(
        GUID(), ForeignKey("users.id"), nullable=True
    )  # Nullable for AI/system messages
    role = Column(
        Enum(MessageRole, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )

    # Content
    content = Column(Text, nullable=False)

    # Metrics
    token_count = Column(Integer, default=0, nullable=False)
    latency_ms = Column(Integer, nullable=True)  # Response latency for observability
    # Time to first token, same clock origin as latency_ms so
    # latency_ms - ttft_ms is the time spent writing the answer. NULL when
    # the turn streamed no token, and on rows predating the column.
    ttft_ms = Column(Integer, nullable=True)
    stopped = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # User stopped this response mid-stream (content is partial)

    # Model information (for AI responses)
    model_name = Column(String(100), nullable=True)
    model_version = Column(String(50), nullable=True)

    # Tool/Function call tracking
    tool_name = Column(String(100), nullable=True)  # If this is a tool message
    tool_call_id = Column(String(255), nullable=True)  # Tool call identifier
    client_message_id = Column(
        GUID(),
        nullable=True,
        doc=(
            "Client-supplied idempotency key for user turns. Combined with thread_id "
            "in a partial unique index to dedupe retries without a SELECT."
        ),
    )
    superseded_by_message_id = Column(
        GUID(),
        # Named explicitly so ``Base.metadata.create_all`` (tests, fresh dev
        # DBs) and Alembic revision e6f7a8b9c0d1 produce the SAME constraint
        # name — an unnamed FK gets an auto-generated name, and a DB built by
        # create_all then re-stamped/upgraded would end up with two.
        ForeignKey(
            "chat_messages.id",
            name="fk_chat_messages_superseded_by_message_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        doc=(
            "Edit-and-resend tombstone. Set to the id of the NEW user row that "
            "replaced this turn: the edited user row itself and every message "
            "after it in the thread point at the replacement. Non-NULL means "
            "'superseded' — every display / model-context / export read filters "
            "IS NULL; by-id, idempotency and analytics reads deliberately do not."
        ),
    )
    tool_executions = Column(JSONB, nullable=True)  # Agent tool execution details

    # Per-turn agent provenance (assistant rows only) — survives page reload.
    # plan: [{step, description, tool, args_hint, depends_on}] from the planner.
    # token_usage: {input_tokens, output_tokens} aggregated across the turn's
    # chat-model calls.
    plan = Column(JSONB, nullable=True)
    token_usage = Column(JSONB, nullable=True)
    # Display-safe status frames emitted while producing this assistant turn.
    progress_steps = Column(JSONB, nullable=True)
    # Planner's top-level rationale for `plan` (capped 2000 chars in planner_node).
    plan_reasoning = Column(Text, nullable=True)
    # Bounded provider-authored reasoning summary. Raw/private reasoning is
    # never persisted or exposed through this field.
    reasoning_summary = Column(Text, nullable=True)

    # Feedback
    feedback_rating = Column(Integer, nullable=True)  # 1-5 rating
    feedback_text = Column(Text, nullable=True)

    # Relationships
    thread = relationship("Thread", back_populates="messages")
    user = relationship("User", foreign_keys=[user_id])
    citations = relationship(
        "Citation",
        back_populates="message",
        cascade="all, delete-orphan",
        order_by=(
            "Citation.source_position.asc().nulls_last(), "
            "Citation.created_at.asc(), Citation.id.asc()"
        ),
    )
    attachments = relationship(
        "MessageAttachment", back_populates="message", cascade="all, delete-orphan"
    )

    def __repr__(self):
        content_preview = (
            self.content[:50] + "..." if len(self.content) > 50 else self.content
        )
        return f"<ChatMessage(role={self.role.value}, content={content_preview})>"

    @property
    def is_user_message(self) -> bool:
        """Check if this is a user message"""
        return self.role == MessageRole.USER

    @property
    def is_assistant_message(self) -> bool:
        """Check if this is an assistant message"""
        return self.role == MessageRole.ASSISTANT

    @property
    def is_system_message(self) -> bool:
        """Check if this is a system message"""
        return self.role == MessageRole.SYSTEM

    @property
    def is_tool_message(self) -> bool:
        """Check if this is a tool message"""
        return self.role == MessageRole.TOOL

    @property
    def has_citations(self) -> bool:
        """Check if message has citations"""
        return len(self.citations) > 0 if self.citations else False

    @property
    def has_attachments(self) -> bool:
        """Check if message has attachments"""
        return len(self.attachments) > 0 if self.attachments else False

    def add_feedback(self, rating: int, text: str = None):
        """Add user feedback to this message"""
        if rating < 1 or rating > 5:
            raise ValueError("Rating must be between 1 and 5")
        self.feedback_rating = rating
        self.feedback_text = text

    def to_dict(
        self, include_citations: bool = False, include_attachments: bool = False
    ) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["role"] = self.role.value if self.role else None
        data["has_citations"] = self.has_citations
        data["has_attachments"] = self.has_attachments

        if include_citations and self.citations:
            data["citations"] = [c.to_dict() for c in self.citations]

        if include_attachments and self.attachments:
            data["attachments"] = [a.to_dict() for a in self.attachments]

        return data

    def to_llm_format(self) -> dict:
        """Convert to format suitable for LLM API calls"""
        return {"role": self.role.value, "content": self.content}

    @classmethod
    def create_user_message(
        cls, thread_id: str, user_id: str, content: str
    ) -> "ChatMessage":
        """Factory method for creating user messages"""
        return cls(
            thread_id=thread_id, user_id=user_id, role=MessageRole.USER, content=content
        )

    @classmethod
    def create_assistant_message(
        cls,
        thread_id: str,
        content: str,
        model_name: str = None,
        token_count: int = 0,
        latency_ms: int = None,
    ) -> "ChatMessage":
        """Factory method for creating assistant messages"""
        return cls(
            thread_id=thread_id,
            role=MessageRole.ASSISTANT,
            content=content,
            model_name=model_name,
            token_count=token_count,
            latency_ms=latency_ms,
        )

    @classmethod
    def create_system_message(cls, thread_id: str, content: str) -> "ChatMessage":
        """Factory method for creating system messages"""
        return cls(thread_id=thread_id, role=MessageRole.SYSTEM, content=content)

    @classmethod
    def create_tool_message(
        cls, thread_id: str, tool_name: str, content: str, tool_call_id: str = None
    ) -> "ChatMessage":
        """Factory method for creating tool messages"""
        return cls(
            thread_id=thread_id,
            role=MessageRole.TOOL,
            content=content,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
