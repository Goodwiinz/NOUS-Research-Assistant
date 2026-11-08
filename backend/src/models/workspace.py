"""
Workspace model for project containers in Terminal Observatory
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class WorkspaceRole(PyEnum):
    """Workspace member roles"""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Workspace(BaseModel):
    """
    Workspace model - project containers for organizing conversations and documents.

    A workspace represents a high-level project or research area where users
    collaborate on conversations, threads, and documents.
    """

    __tablename__ = "workspaces"

    # Basic information
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)

    # Access control
    is_archived = Column(Boolean, default=False, nullable=False)
    is_public = Column(Boolean, default=False, nullable=False)

    # Owner (creator)
    owner_id = Column(GUID(), ForeignKey("users.id"), nullable=False)

    # Organization scope (optional - workspaces can be org-scoped or personal)
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=True)

    # Database indexes for performance optimization
    __table_args__ = (
        Index("idx_workspace_owner_archived", "owner_id", "is_archived"),
        # Plain indexes created by migrations; declared so the models match
        # what ``alembic upgrade head`` builds. They duplicate each other and
        # the index above; all exist in every migrated database.
        # a1b2c3d4e5f6_add_thread_centric_chat_schema:
        Index("ix_workspaces_owner_id", "owner_id"),
        Index("ix_workspaces_organization_id", "organization_id"),
        # n9p2q3r4s5t6_add_missing_performance_indexes:
        Index("idx_workspaces_owner", "owner_id"),
        Index("idx_workspaces_organization", "organization_id"),
        Index("idx_workspaces_owner_archived", "owner_id", "is_archived"),
    )

    # Relationships
    owner = relationship("User", foreign_keys=[owner_id])
    organization = relationship("Organization")
    members = relationship(
        "WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan"
    )
    conversations = relationship(
        "Conversation", back_populates="workspace", cascade="all, delete-orphan"
    )
    collections = relationship(
        "Collection", back_populates="workspace", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Workspace(name={self.name}, owner_id={self.owner_id})>"

    def _active_members(self):
        """Return memberships that have not been soft-deleted."""
        return [m for m in self.members if not getattr(m, "is_deleted", False)]

    def is_member(self, user_id: str) -> bool:
        """Check if user is a member of this workspace"""
        return any(str(m.user_id) == str(user_id) for m in self._active_members())

    def get_member_role(self, user_id: str) -> WorkspaceRole:
        """Get user's role in this workspace"""
        for member in self._active_members():
            if str(member.user_id) == str(user_id):
                return member.role
        return None

    def can_user_edit(self, user_id: str) -> bool:
        """Check if user can edit this workspace"""
        role = self.get_member_role(user_id)
        if role is None:
            return str(self.owner_id) == str(user_id)
        return role in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN, WorkspaceRole.EDITOR]

    def can_user_admin(self, user_id: str) -> bool:
        """Check if user can administer this workspace"""
        role = self.get_member_role(user_id)
        if role is None:
            return str(self.owner_id) == str(user_id)
        return role in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["member_count"] = len(self.members) if self.members else 0
        data["conversation_count"] = (
            len(self.conversations) if self.conversations else 0
        )
        return data


class WorkspaceMember(BaseModel):
    """
    Workspace membership model.

    Tracks which users have access to a workspace and their role.
    """

    __tablename__ = "workspace_members"

    workspace_id = Column(
        GUID(),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(
        Enum(
            WorkspaceRole,
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
            name="workspacerole",
        ),
        nullable=False,
        default=WorkspaceRole.VIEWER,
    )

    # Tracking
    joined_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    invited_by_id = Column(GUID(), ForeignKey("users.id"), nullable=True)

    # Database indexes and constraints
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        # The same key under the name a1b2c3d4e5f6_add_thread_centric_chat_schema
        # gave it; every migrated database carries both constraints.
        UniqueConstraint(
            "workspace_id", "user_id", name="uq_workspace_members_workspace_user"
        ),
    )

    # Relationships
    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])
    invited_by = relationship("User", foreign_keys=[invited_by_id])

    def __repr__(self):
        return f"<WorkspaceMember(workspace_id={self.workspace_id}, user_id={self.user_id}, role={self.role.value})>"

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["role"] = self.role.value if self.role else None
        return data
