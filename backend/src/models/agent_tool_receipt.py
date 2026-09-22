"""Durable receipt for one executed side-effecting tool call (audit B8-I1).

``tool_node`` commits tool side effects before any checkpoint write, and the
only replay guard is the per-turn ``state["tool_executions"]`` list, which is
lost together with the node. A turn resumed from the pre-``tool_node``
checkpoint therefore re-runs the call — creating a second project, note,
draft, arXiv ingest or sandbox execution.

One row per ``tool_call_id`` (the id the model itself generated for the call,
stable across a replay of the same checkpoint) makes that guard durable. The
table is deliberately tiny and write-once: no updates, no soft delete, no
relationships.
"""

from sqlalchemy import Column, DateTime, String, func

from .base import Base


class AgentToolReceipt(Base):
    """Receipt that ``tool_call_id`` already ran to completion."""

    __tablename__ = "agent_tool_receipts"

    # The LLM-generated tool call id. Primary key: the whole point is that a
    # second INSERT for the same id conflicts.
    tool_call_id = Column(String(128), primary_key=True)

    # Correlation only, for operators reading the table — plain text rather
    # than a FK to threads, because the value comes from the LangGraph
    # configurable as a raw string and may be absent or non-uuid. A receipt
    # must never fail to write because a correlation id looked odd.
    thread_id = Column(String(64), nullable=True, index=True)

    tool_name = Column(String(64), nullable=False)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
