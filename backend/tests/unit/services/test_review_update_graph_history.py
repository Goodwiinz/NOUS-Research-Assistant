"""Accepted publication evidence survives later incomplete review deltas."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.research import release_rules
from src.services.research_engine import review_update_service as reviews

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("history", "affected", "merged"),
    [
        ([], False, False),
        (["unknown", "corrected_retracted"], True, False),
        ([None, "corrected_retracted"], True, False),
        (["unchanged", "corrected_retracted"], True, False),
        (["unknown", "changed"], False, False),
        (["changed"], True, False),
        (["unknown", "corrected_retracted"], True, True),
    ],
    ids=[
        "no-accepted-delta",
        "unknown-retains-notice",
        "omitted-report-retains-notice",
        "later-no-notice-retains-known-notice",
        "ordinary-change-stays-tip-only",
        "latest-ordinary-change",
        "notice-follows-merged-report",
    ],
)
async def test_release_graph_retains_known_publication_evidence(
    monkeypatch: pytest.MonkeyPatch,
    history: list[str | None],
    affected: bool,
    merged: bool,
) -> None:
    collection, report, survivor, document, unrelated_document = (
        uuid4() for _ in range(5)
    )
    canonical = survivor if merged else report
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = [
        {"items": ([] if kind is None else [{"report_id": str(report), "class": kind}])}
        for kind in history
    ]
    monkeypatch.setattr(
        reviews.search_update_service,
        "_merged_into",
        AsyncMock(return_value={str(report): str(survivor)} if merged else {}),
    )
    monkeypatch.setattr(
        reviews,
        "document_reports",
        AsyncMock(return_value={document: canonical, unrelated_document: uuid4()}),
    )

    edges, changed = await reviews.graph_part(db, collection)

    expected = release_rules.node("report", canonical)
    assert changed == ({expected} if affected else set())
    assert edges == (
        [(expected, release_rules.node("document", document))] if affected else []
    )
