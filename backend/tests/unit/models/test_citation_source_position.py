from __future__ import annotations

from uuid import uuid4

import pytest

from src.models.chat_message import ChatMessage
from src.models.citation import Citation

pytestmark = pytest.mark.unit


def test_citation_source_position_is_exposed_to_reload_consumers() -> None:
    citation = Citation(
        id=uuid4(),
        document_title="Paper",
        snippet="evidence",
        chunk_id="chunk-2",
        source_position=2,
    )
    frontend = citation.to_frontend_format()
    assert frontend["source_position"] == 2
    assert frontend["chunk_id"] == "chunk-2"


def test_message_citations_relationship_orders_positioned_before_legacy() -> None:
    order = ChatMessage.citations.property.order_by
    rendered = " ".join(str(item) for item in order)
    assert "citations.source_position ASC NULLS LAST" in rendered
    assert "citations.created_at ASC" in rendered
    assert "citations.id ASC" in rendered
