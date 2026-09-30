"""GOO-305 anchored extraction writes, evidence reads and the accept guard.

The session is faked and the forms-service lookups are patched, so these pin
the service's decisions; ``tests/integration/test_extraction_anchor_postgres.py``
proves the same paths on PostgreSQL.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from src.models.extraction_matrix import ExtractionAcceptedValue, ExtractionObservation
from src.models.research_project_role import ResearchProjectRole
from src.services.research import extraction_forms_service as svc
from src.services.research import source_anchors as anchors
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionObservationCreate,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
TEXT = (
    "[Page 1]\nWe enrolled 120 adults. Participants were randomized. "
    "Participants were randomized.\n[Page 2]\nOutcome at 12 weeks."
)
CHECKSUM = "a" * 64
FIELD = {"field_id": str(uuid4()), "name": "Sample size", "type": "text"}


class _Rows(list[Any]):
    def all(self) -> list[Any]:
        return list(self)


def _result(scalar: Any = None, rows: list[Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalars.return_value = _Rows(rows or [])
    return result


def _document(text: str | None = TEXT, checksum: str = CHECKSUM) -> Any:
    return SimpleNamespace(id=uuid4(), content_text=text, checksum_sha256=checksum)


def _pins(document: Any) -> tuple[str, str]:
    return svc.document_pins(document)


class _World(SimpleNamespace):
    db: AsyncMock
    appended: list[dict[str, Any]]
    document: Any
    matrix: Any
    version: Any
    context: Any
    tips: list[Any]
    staled: set[str]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    db = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock(side_effect=lambda row: setattr(row, "created_at", NOW))
    document = _document()
    matrix = SimpleNamespace(id=uuid4(), project_id=uuid4())
    version = SimpleNamespace(
        id=uuid4(), fields=[FIELD], content_hash="c" * 64, matrix_id=matrix.id
    )
    context = SimpleNamespace(
        collection=SimpleNamespace(id=matrix.project_id),
        effective_roles=frozenset(
            {ResearchProjectRole.REVIEWER, ResearchProjectRole.ADJUDICATOR}
        ),
    )
    w = _World(
        db=db,
        appended=[],
        document=document,
        matrix=matrix,
        version=version,
        context=context,
        tips=[],
        staled=set(),
    )

    async def append(_db: Any, **kwargs: Any) -> None:
        w.appended.append(kwargs)

    async def get_document(_db: Any, _context: Any, document_id: UUID) -> Any:
        if document_id != w.document.id:
            raise HTTPException(404, svc.DOCUMENT_NOT_FOUND)
        return w.document

    monkeypatch.setattr(svc, "_append", append)
    monkeypatch.setattr(svc, "_matrix", AsyncMock(return_value=matrix))
    monkeypatch.setattr(svc, "_lock", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(svc, "_replayed_event", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_document", get_document)
    monkeypatch.setattr(svc, "_current_field", AsyncMock(return_value=(version, FIELD)))
    monkeypatch.setattr(svc, "_flush_or_conflict", AsyncMock())
    monkeypatch.setattr(svc, "_versions", AsyncMock(return_value=[version]))
    monkeypatch.setattr(svc, "_tips", AsyncMock(side_effect=lambda *_: w.tips))
    monkeypatch.setattr(svc, "_staled_ids", AsyncMock(side_effect=lambda *_: w.staled))
    return w


def _observation(
    w: _World,
    value: Any = "120",
    quote: str | None = "We enrolled 120 adults.",
    **kw: Any,
) -> ExtractionObservation:
    anchor = kw.pop("anchor", None) or anchors.verify_anchor(TEXT, quote)
    source_hash, text_hash = kw.pop("pins", _pins(w.document))
    return ExtractionObservation(
        id=kw.pop("id", uuid4()),
        form_version_id=w.version.id,
        field_id=UUID(FIELD["field_id"]),
        document_id=w.document.id,
        kind=kw.pop("kind", "machine"),
        actor_user_id=uuid4(),
        value=value,
        missingness=None,
        validation_state="valid",
        citation=quote,
        source_hash=source_hash,
        text_sha256=text_hash,
        created_at=kw.pop("created_at", NOW),
        **svc._anchor_columns(anchor),
        **kw,
    )


def _tip(w: _World, pins: tuple[str, str]) -> ExtractionAcceptedValue:
    return ExtractionAcceptedValue(
        id=uuid4(),
        form_version_id=w.version.id,
        field_id=UUID(FIELD["field_id"]),
        document_id=w.document.id,
        value="120",
        observation_ids=[],
        accepted_by_id=uuid4(),
        rationale="r",
        source_hash=pins[0],
        text_sha256=pins[1],
        created_at=NOW,
    )


def _accept_body(w: _World, cited: list[Any], **kw: Any) -> ExtractionAcceptCreate:
    return ExtractionAcceptCreate(
        document_id=w.document.id,
        field_id=UUID(FIELD["field_id"]),
        form_version_id=w.version.id,
        observation_ids=[o.id for o in cited],
        value=kw.pop("value", "120"),
        rationale="matches methods",
        idempotency_key=kw.pop("key", "a1"),
        **kw,
    )


async def _accept(w: _World, cited: list[Any], **kw: Any) -> Any:
    tip = kw.get("supersedes_accepted_value_id")
    w.db.execute = AsyncMock(side_effect=[_result(tip), _result(rows=cited)])
    return await svc.accept_value(
        w.db, w.context, w.matrix.id, uuid4(), _accept_body(w, cited, **kw)
    )


async def _status(code: int, call: Any) -> str:
    with pytest.raises(HTTPException) as error:
        await call
    assert error.value.status_code == code, error.value.detail
    return str(error.value.detail)


def _added(w: _World, model: type) -> list[Any]:
    return [c.args[0] for c in w.db.add.call_args_list if isinstance(c.args[0], model)]


async def test_worker_writes_anchor_coverage_text_hash_and_no_confidence(
    world: _World,
) -> None:
    async def read(chunk: str) -> dict[str, Any]:
        return {"Sample size": {"value": "120", "citation": "We enrolled 120 adults."}}

    observations, coverage, complete = await anchors.read_whole_text(
        TEXT, [FIELD], read
    )
    await svc.append_machine_observations(
        world.db,
        matrix=world.matrix,
        version=world.version,
        document=world.document,
        observations=observations,
        coverage=coverage,
        actor_id=uuid4(),
        run_id="task-1",
        model="stub",
    )
    (row,) = _added(world, ExtractionObservation)
    assert (row.anchor_status, row.anchor_page) == ("verified", 1)
    assert TEXT[row.anchor_start_char : row.anchor_end_char] == row.citation
    assert row.text_sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
    assert (row.inspected_coverage, row.text_length) == ([[0, len(TEXT)]], len(TEXT))
    assert not hasattr(row, "confidence")
    (event,) = world.appended
    assert (event["event_type"], event["version"]) == ("extraction.observed", 2)
    anchor = event["payload"]["observations"][str(row.id)]
    assert anchor["anchor_status"] == "verified" and "confidence" not in anchor
    assert (
        anchor["citation_sha256"] == hashlib.sha256(row.citation.encode()).hexdigest()
    )
    assert event["payload"]["inspected_coverage"] == [[0, len(TEXT)]]


async def test_worker_rerun_after_text_change_appends_staled_then_observations(
    world: _World,
) -> None:
    old_pins = (CHECKSUM, "0" * 64)  # same checksum, older text
    tip = _tip(world, old_pins)
    world.tips = [tip, _tip(world, _pins(world.document))]  # second: current
    observations = {FIELD["field_id"]: anchors.aggregate(FIELD["field_id"], [], True)}
    await svc.append_machine_observations(
        world.db,
        matrix=world.matrix,
        version=world.version,
        document=world.document,
        observations=observations,
        coverage=[(0, len(TEXT))],
        actor_id=uuid4(),
        run_id="task-2",
        model="stub",
    )
    staled, observed = world.appended
    assert (staled["event_type"], staled["actor_role"]) == (
        "extraction.staled",
        "machine",
    )
    assert staled["payload"]["accepted_value_ids"] == [str(tip.id)]
    assert staled["payload"]["reason"] == "source_changed"
    assert staled["payload"]["new_text_sha256"] == _pins(world.document)[1]
    assert observed["event_type"] == "extraction.observed"
    (row,) = _added(world, ExtractionObservation)
    assert (row.missingness, row.anchor_status) == ("not_reported", None)


async def test_list_observations_foreign_org_document_404_no_citation(
    world: _World,
) -> None:
    world.db.execute = AsyncMock()
    detail = await _status(
        404,
        svc.list_observations(
            world.db, world.context, world.matrix.id, uuid4(), UUID(FIELD["field_id"])
        ),
    )
    assert detail == svc.DOCUMENT_NOT_FOUND
    world.db.execute.assert_not_awaited()  # nothing about the cell was read


async def test_list_observations_detached_document_404(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A detached document is invisible to project_documents_query; the real
    # _document is exercised here against a result with no row.
    monkeypatch.undo()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(None))
    await _status(404, svc._document(db, world.context, world.document.id))
    assert db.execute.await_args is not None
    sql = str(db.execute.await_args.args[0]).lower()
    assert "collection_documents.is_deleted" in sql
    assert "documents.organization_id" in sql


async def test_list_observations_source_changed_hides_context(world: _World) -> None:
    fresh = _observation(world)
    stale = _observation(world, pins=(CHECKSUM, "0" * 64))
    world.db.execute = AsyncMock(
        side_effect=[_result(rows=[fresh, stale]), _result(rows=[])]
    )
    listing = await svc.list_observations(
        world.db,
        world.context,
        world.matrix.id,
        world.document.id,
        UUID(FIELD["field_id"]),
    )
    current, changed = listing.observations
    assert current.source_changed is False and current.anchor is not None
    assert current.anchor.page == 1 and current.context_after is not None
    assert current.context_before is not None
    assert current.context_before.endswith("[Page 1]\n")
    assert changed.source_changed is True
    assert (changed.context_before, changed.context_after) == (None, None)


async def test_accept_ambiguous_without_start_409(world: _World) -> None:
    ambiguous = _observation(world, "randomized", "Participants were randomized.")
    assert ambiguous.anchor_status == "ambiguous"
    detail = await _status(409, _accept(world, [ambiguous], value="randomized"))
    assert detail == svc.ANCHOR_AMBIGUOUS
    assert _added(world, ExtractionAcceptedValue) == []


async def test_accept_ambiguous_with_recorded_start_disambiguated(
    world: _World,
) -> None:
    ambiguous = _observation(world, "randomized", "Participants were randomized.")
    second = ambiguous.anchor_occurrences[1]
    detail = await _status(
        409,
        _accept(world, [ambiguous], value="randomized", anchor_start=second + 1),
    )
    assert detail == svc.ANCHOR_AMBIGUOUS  # not a recorded occurrence
    accepted = await _accept(
        world, [ambiguous], value="randomized", anchor_start=second, key="a2"
    )
    assert accepted.anchor_resolution == "disambiguated"
    assert (accepted.anchor_start_char, accepted.anchor_observation_id) == (
        second,
        ambiguous.id,
    )
    event = world.appended[-1]
    assert event["version"] == 2
    assert event["payload"]["anchor_resolution"] == "disambiguated"


async def test_accept_unverified_requires_flag(world: _World) -> None:
    ocr = _observation(world, "120", "We enro1led 120 adults.")
    assert ocr.anchor_status == "unverified"
    detail = await _status(409, _accept(world, [ocr]))
    assert detail == svc.ANCHOR_UNVERIFIED
    accepted = await _accept(world, [ocr], accept_unverified=True, key="a2")
    assert accepted.anchor_resolution == "accepted_unverified"
    assert accepted.anchor_start_char is None


async def test_accept_after_source_change_commits_staled_then_409(
    world: _World,
) -> None:
    old = (CHECKSUM, "0" * 64)
    tip = _tip(world, old)
    world.tips = [tip]
    cited = _observation(world, pins=old)
    detail = await _status(
        409, _accept(world, [cited], supersedes_accepted_value_id=tip.id)
    )
    assert detail == svc.SOURCE_CHANGED
    (staled,) = world.appended
    assert staled["payload"]["accepted_value_ids"] == [str(tip.id)]
    assert staled["actor_role"] == "adjudicator"
    world.db.commit.assert_awaited_once()
    assert _added(world, ExtractionAcceptedValue) == []


async def test_human_override_observation_verified_then_accepted(
    world: _World,
) -> None:
    machine = _observation(world, "120", "Participants were randomized.")
    before = dict(vars(machine))
    override = await svc.observe(
        world.db,
        world.context,
        world.matrix.id,
        uuid4(),
        ExtractionObservationCreate(
            document_id=world.document.id,
            field_id=UUID(FIELD["field_id"]),
            form_version_id=world.version.id,
            value="120",
            citation="Participants were randomized.",
            anchor_start=TEXT.rindex("Participants were randomized."),
            idempotency_key="o1",
        ),
    )
    assert override.anchor is not None and override.anchor.status == "verified"
    assert override.anchor.occurrences_in_text == 2
    (row,) = _added(world, ExtractionObservation)
    row.created_at = NOW + timedelta(seconds=1)
    accepted = await _accept(world, [machine, row], key="a9")
    assert accepted.anchor_resolution == "verified"
    assert accepted.anchor_observation_id == row.id  # verified beats ambiguous
    assert {k: v for k, v in vars(machine).items() if k in before} == before


async def test_legacy_cell_view_is_legacy_unanchored_with_uncalibrated_confidence(
    world: _World,
) -> None:
    legacy = SimpleNamespace(
        document_id=world.document.id,
        column_name="Sample size",
        value="twelve",
        citation_snippet="p1",
        confidence=0.8,
    )
    world.db.execute = AsyncMock(
        side_effect=[
            _result(rows=[legacy]),
            _result(rows=[]),
            _result(rows=[world.document]),
        ]
    )
    world.version.provenance = "legacy_unversioned"
    world.version.version_no, world.version.created_at = 1, NOW
    world.version.protocol_version_id = None
    _, cells = await svc.cell_view(world.db, world.matrix, [world.document.id])
    (cell,) = cells
    assert cell["source"] == "legacy"
    assert cell["anchor_status"] == "legacy_unanchored"
    assert (cell["confidence"], cell["confidence_calibration"]) == (0.8, "uncalibrated")
