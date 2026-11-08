"""
MessageAttachment model for Terminal Observatory multimodal messages
"""

from sqlalchemy import Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class MessageAttachment(BaseModel):
    """
    MessageAttachment model - links documents/files to messages.

    Allows users to attach documents (images, files, etc.) to their messages
    for multimodal interactions with the AI assistant.
    """

    __tablename__ = "message_attachments"
    __table_args__ = (
        # Created by a1b2c3d4e5f6_add_thread_centric_chat_schema: a document is
        # attached to a message at most once.
        UniqueConstraint("message_id", "document_id", name="uq_message_attachments"),
    )

    # Parent relationships
    message_id = Column(
        GUID(),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(
        GUID(),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional metadata
    display_name = Column(String(255), nullable=True)  # Custom display name
    thumbnail_url = Column(String(1000), nullable=True)  # Thumbnail for images/videos

    # Relationships
    message = relationship("ChatMessage", back_populates="attachments")
    document = relationship("Document")

    def __repr__(self):
        return f"<MessageAttachment(message_id={self.message_id}, document_id={self.document_id})>"

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()

        # Include document info if available
        if self.document:
            data["document_title"] = self.document.title
            data["document_type"] = (
                self.document.document_type.value
                if self.document.document_type
                else None
            )
            data["mime_type"] = self.document.mime_type

        return data

    def to_frontend_format(self) -> dict:
        """Convert to format suitable for frontend display"""
        return {
            "id": str(self.id),
            "document_id": str(self.document_id),
            "display_name": self.display_name
            or (self.document.title if self.document else "Unknown"),
            "thumbnail_url": self.thumbnail_url,
            "document_type": (
                self.document.document_type.value
                if self.document and self.document.document_type
                else None
            ),
            "mime_type": self.document.mime_type if self.document else None,
        }

    class Meta:
        unique_together = [("message_id", "document_id")]
