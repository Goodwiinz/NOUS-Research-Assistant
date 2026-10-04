"""Unit helpers behind GOO-309 appraisal: the analysis unit and document ->
report mapping (the PostgreSQL proof covers the service end to end)."""

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from src.services.research_engine import acquisition_service
from src.services.research_engine.identity_service import analysis_unit

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rows: list[tuple[UUID, UUID]]) -> None:
        self.rows = rows

    def tuples(self) -> "_Result":
        return self

    def all(self) -> list[tuple[UUID, UUID]]:
        return self.rows


class _Session:
    """Answers the two queries in order: retrieved heads, then merges."""

    def __init__(self, *answers: list[tuple[UUID, UUID]]) -> None:
        self.answers = list(answers)

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.answers.pop(0))


@pytest.mark.asyncio
async def test_document_reports_follows_merge_chain() -> None:
    d1, d2, r1, r2, r3, r4 = (uuid4() for _ in range(6))
    # r1 merged into r2, r2 into r3; r4 survives. A cycle cannot hang.
    session = _Session([(d1, r1), (d2, r4)], [(r1, r2), (r2, r3)])
    mapping = await acquisition_service.document_reports(cast(Any, session), uuid4())
    assert mapping == {d1: r3, d2: r4}
    assert (
        await acquisition_service.document_reports(cast(Any, _Session([])), uuid4())
        == {}
    )
    assert acquisition_service._final_report({r1: r2, r2: r1}, r1) in {r1, r2}


def test_analysis_unit_confirmed_none_proposed() -> None:
    report, study = uuid4(), uuid4()

    def unit(status: str | None) -> str | None:
        return analysis_unit(
            SimpleNamespace(
                id=report,
                study_id=None if status is None else study,
                study_link_status=status,
            )
        )

    assert unit("confirmed") == f"study:{study}"
    assert unit(None) == f"report:{report}"
    assert unit("proposed") is None
    assert unit("disputed") is None
