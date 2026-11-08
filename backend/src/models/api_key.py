"""API key models.

R6-L12: these tables previously lived beside their auth logic in
src/core/api_key_auth.py, registered against a different Base import path
and invisible to ``src.models`` metadata (create_all, alembic autogenerate,
and the drift checker all missed them). They now live here like every other
model; api_key_auth re-exports them for backwards compatibility.
"""

import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from .base import Base


class APIKey(Base):
    """API Key model for public endpoint access"""

    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(16))
    name = Column(String, nullable=False)  # Human readable name
    key_hash = Column(String, nullable=False, unique=True)  # Hashed API key
    key_prefix = Column(String(8), nullable=False)  # First 8 chars for identification
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    usage_count = Column(Integer, default=0, nullable=False)
    rate_limit_per_hour = Column(
        Integer, default=100, nullable=False
    )  # Requests per hour
    allowed_endpoints = Column(Text, nullable=True)  # JSON array of allowed endpoints
    created_by = Column(String, nullable=True)  # Admin who created the key
    description = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)  # Optional expiration
    organization_id = Column(String, nullable=True)  # Organization the key is scoped to

    # Created by add_api_keys_table (organization: add_org_id_api_keys).
    # idx_api_keys_hash duplicates the unique constraint on key_hash.
    __table_args__ = (
        Index("idx_api_keys_hash", "key_hash"),
        Index("idx_api_keys_active", "is_active"),
        Index("idx_api_keys_prefix", "key_prefix"),
        Index("idx_api_keys_created_at", "created_at"),
        Index("idx_api_keys_last_used", "last_used_at"),
        Index("idx_api_keys_organization", "organization_id"),
    )


class APIKeyUsageLog(Base):
    """API Key usage log model for audit trail"""

    __tablename__ = "api_key_usage_log"

    id = Column(String, primary_key=True, default=lambda: secrets.token_hex(16))
    # R6-F7: the historical add_api_keys_table migration declares this FK, but
    # it is skipped on fresh databases (the baseline creates the table first),
    # so the model must declare it too or the two schemas diverge.
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=False)
    endpoint = Column(String(255), nullable=False)  # API endpoint accessed
    method = Column(String(10), nullable=False)  # HTTP method used
    client_ip = Column(String(45), nullable=True)  # Client IP address
    user_agent = Column(Text, nullable=True)  # Client user agent
    request_size_bytes = Column(Integer, nullable=True)  # Request payload size
    response_status = Column(Integer, nullable=True)  # HTTP response status
    response_time_ms = Column(Integer, nullable=True)  # Response time in milliseconds
    search_query = Column(Text, nullable=True)  # Search query for search endpoints
    results_count = Column(Integer, nullable=True)  # Number of results returned
    error_message = Column(Text, nullable=True)  # Error message if request failed
    accessed_at = Column(
        DateTime, default=datetime.utcnow, nullable=False
    )  # When the API was accessed

    # Created by add_api_keys_table.
    __table_args__ = (
        Index("idx_api_usage_key_id", "api_key_id"),
        Index("idx_api_usage_accessed_at", "accessed_at"),
        Index("idx_api_usage_endpoint", "endpoint"),
        Index("idx_api_usage_client_ip", "client_ip"),
    )
