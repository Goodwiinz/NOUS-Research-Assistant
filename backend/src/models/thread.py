"""
Thread model for Terminal Observatory thread-centric chat schema
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class ThreadStatus(PyEnum):
    """Thread status states"""

    ACTIVE = "active"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class Thread(BaseModel):
    """
    Thread model - granular line of inquiry within a conversation.

    A thread represents a specific investigation or topic (e.g., "Revenue Forecast Investigation")
    that contains messages between the user and the AI assistant.
    """

    __tablename__ = "threads"

    # Parent relationship
    conversation_id = Column(
        GUID(),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Basic information
    title = Column(String(500), nullable=True)  # Optional - can be auto-generated
    summary = Column(Text, nullable=True)  # AI-generated summary of thread

    # State - Use values_callable to use lowercase enum values matching the database
    status = Column(
        Enum(
            ThreadStatus,
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
            name="threadstatus",
        ),
        nullable=False,
        default=ThreadStatus.ACTIVE,
    )

    # Metadata
    last_message_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    message_count = Column(Integer, default=0, nullable=False)
    token_count = Column(Integer, default=0, nullable=False)

    # Creator tracking
    created_by_id = Column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Project integration (optional)
    source_project_id = Column(
        GUID(),
        ForeignKey("collections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Project that originated this thread (if started from project)",
    )
    rag_document_scope = Column(
        JSONB,
        nullable=True,
        comment='Document IDs for RAG filtering. Format: {"document_ids": ["uuid1", "uuid2"]}',
    )
    message_branching = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    branch_document_id = Column(
        GUID(),
        ForeignKey(
            "documents.id", name="fk_threads_branch_document_id", ondelete="SET NULL"
        ),
        nullable=True,
    )
    selected_message_id = Column(
        GUID(),
        ForeignKey(
            "chat_messages.id",
            name="fk_threads_selected_message_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )

    # Database indexes for performance optimization
    __table_args__ = (
        Index("idx_thread_conversation_status", "conversation_id", "status"),
        Index("idx_thread_conversation_created", "conversation_id", "created_at"),
    )

    # Relationships
    conversation = relationship("Conversation", back_populates="threads")
    created_by = relationship("User", foreign_keys=[created_by_id])
    messages = relationship(
        "ChatMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at.asc()",
        foreign_keys="ChatMessage.thread_id",
    )
    source_project = relationship("Collection", foreign_keys=[source_project_id])

    def __repr__(self):
        return f"<Thread(title={self.title}, status={self.status.value}, conversation_id={self.conversation_id})>"

    def update_message_stats(self, token_count: int = 0):
        """Update message count and last message timestamp"""
        self.message_count = len(self.messages) if self.messages else 0
        self.last_message_at = datetime.utcnow()
        if token_count > 0:
            self.token_count += token_count

    def resolve(self):
        """Mark thread as resolved"""
        self.status = ThreadStatus.RESOLVED

    def reopen(self):
        """Reopen a resolved thread"""
        self.status = ThreadStatus.ACTIVE

    def archive(self):
        """Archive the thread"""
        self.status = ThreadStatus.ARCHIVED

    @property
    def is_active(self) -> bool:
        """Check if thread is active"""
        return self.status == ThreadStatus.ACTIVE

    @property
    def is_resolved(self) -> bool:
        """Check if thread is resolved"""
        return self.status == ThreadStatus.RESOLVED

    def to_dict(self, include_messages: bool = False) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["status"] = self.status.value if self.status else None
        data["message_count"] = self.message_count
        data["token_count"] = self.token_count
        data["source_project_id"] = self.source_project_id
        data["rag_document_scope"] = self.rag_document_scope

        if include_messages and self.messages:
            data["messages"] = [m.to_dict() for m in self.messages]

        return data

    def generate_title(self) -> str:
        """Generate a smart title from the first user message.

        Uses heuristics to create meaningful titles:
        - Removes greeting patterns
        - Extracts question topics
        - Title cases the result
        """
        if self.title:
            return self.title

        # Accessing self.messages triggers a lazy load when the relationship
        # isn't eager-loaded. Under the async engine that raises MissingGreenlet
        # (-> 500), which is how an *untitled* thread broke link/list endpoints
        # that build a title via `thread.title or thread.generate_title()`.
        # Only consult messages when they're already loaded; otherwise fall
        # back to an id-based title instead of forcing a query.
        from sqlalchemy import inspect as sa_inspect

        if "messages" not in sa_inspect(self).unloaded and self.messages:
            from ..services.threads.thread_title_generator import generate_title_sync
            from .chat_message import MessageRole

            for msg in self.messages:
                if msg.role == MessageRole.USER and msg.content:
                    return generate_title_sync(msg.content)

        return f"Thread {str(self.id)[:8]}"
