"""
Conversation model for Terminal Observatory thread-centric chat schema
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import joinedload, relationship, selectinload

from .base import GUID, BaseModel


class Conversation(BaseModel):
    """
    Conversation model - high-level container for related threads.

    A conversation represents a project or topic area (e.g., "Q3 Financial Analysis",
    "Project Typhoon") that contains multiple threads of inquiry.
    """

    __tablename__ = "conversations"

    # Basic information
    workspace_id = Column(
        GUID(),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(500), nullable=False, index=True)
    description = Column(Text, nullable=True)

    # State
    is_archived = Column(Boolean, default=False, nullable=False)
    is_pinned = Column(Boolean, default=False, nullable=False)

    # Metadata
    last_activity_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    # Creator tracking
    created_by_id = Column(GUID(), ForeignKey("users.id"), nullable=False)

    # Relationships
    workspace = relationship("Workspace", back_populates="conversations")
    created_by = relationship("User", foreign_keys=[created_by_id])
    threads = relationship(
        "Thread",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Thread.created_at.desc()",
    )

    # Database indexes for performance optimization
    __table_args__ = (
        Index(
            "idx_conversation_workspace_activity", "workspace_id", "last_activity_at"
        ),
        Index("idx_conversation_creator_created", "created_by_id", "created_at"),
        Index("idx_conversation_workspace_archived", "workspace_id", "is_archived"),
        Index("idx_conversation_pinned_activity", "is_pinned", "last_activity_at"),
        Index("idx_conversation_title_search", "title"),
        # Created by a1b2c3d4e5f6_add_thread_centric_chat_schema.
        Index("ix_conversations_last_activity", "last_activity_at"),
    )

    def __repr__(self):
        return f"<Conversation(title={self.title}, workspace_id={self.workspace_id})>"

    @classmethod
    def get_with_workspace_and_user(cls, conversation_id):
        """Get conversation with workspace and creator eagerly loaded"""
        return (
            cls.query.options(joinedload(cls.workspace), joinedload(cls.created_by))
            .filter(cls.id == conversation_id)
            .first()
        )

    @classmethod
    def get_workspace_conversations_with_details(cls, workspace_id, limit=50):
        """Get workspace conversations with threads and creator loaded"""
        from src.models.thread import Thread

        return (
            cls.query.options(
                joinedload(cls.created_by),
                selectinload(cls.threads).options(joinedload(Thread.created_by)),
            )
            .filter(cls.workspace_id == workspace_id, cls.is_archived == False)
            .order_by(cls.last_activity_at.desc())
            .limit(limit)
            .all()
        )

    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity_at = datetime.utcnow()

    @property
    def thread_count(self) -> int:
        """Get number of threads"""
        return len(self.threads) if self.threads else 0

    @property
    def active_thread_count(self) -> int:
        """Get number of active threads"""
        if not self.threads:
            return 0
        from .thread import ThreadStatus

        return sum(1 for t in self.threads if t.status == ThreadStatus.ACTIVE)

    def to_dict(self, include_threads: bool = False) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["thread_count"] = self.thread_count
        data["active_thread_count"] = self.active_thread_count

        if include_threads and self.threads:
            data["threads"] = [
                t.to_dict() for t in self.threads[:10]
            ]  # Limit to first 10

        return data

    def archive(self):
        """Archive the conversation"""
        self.is_archived = True

    def unarchive(self):
        """Unarchive the conversation"""
        self.is_archived = False
