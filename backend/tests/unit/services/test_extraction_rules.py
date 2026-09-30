"""Pure extraction-form rules (GOO-304): field ids, hashes, types, missingness,
staleness, read precedence and what an acceptance may say."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services.research import extraction_rules as rules
from src.services.research.extraction_rules import Accepted, LegacyCell, Obs

pytestmark = pytest.mark.unit

H1, H2 = "a" * 64, "b" * 64


def _fields(matrix_id: UUID, *columns: dict) -> list[dict]:
    return rules.build_fields(matrix_id, list(columns))


def _obs(
    value: object = None,
    missingness: str | None = None,
    state: str = "valid",
    kind: str = "machine",
) -> Obs:
    return Obs(
        id=uuid4(),
        kind=kind,
        value=value,
        missingness=missingness,
        validation_state=state,
        form_version_id=uuid4(),
        citation=None,
        source_hash=H1,
        created_at=datetime.now(timezone.utc),
    )


def test_field_id_stable_across_versions_and_distinct_across_matrices() -> None:
    m1, m2 = uuid4(), uuid4()
    assert rules.field_id(m1, "Sample size") == rules.field_id(m1, "Sample size")
    assert rules.field_id(m1, "Sample size") != rules.field_id(m2, "Sample size")
    assert rules.field_id(m1, "Sample size") != rules.field_id(m1, "Design")
    v1 = _fields(m1, {"name": "Sample size"})
    v2 = _fields(m1, {"name": "Design"}, {"name": "Sample size", "type": "number"})
    assert v1[0]["field_id"] == v2[1]["field_id"]
    assert v2[1]["type"] == "number" and v1[0]["type"] == "text"
    # The frozen namespace is part of the migration contract.
    assert rules.FIELD_NAMESPACE == UUID("ffb3dc50-fe01-4981-93f4-b1d1f49683c5")


def test_build_fields_rejects_bad_definitions() -> None:
    m = uuid4()
    with pytest.raises(ValueError, match="categories"):
        _fields(m, {"name": "Arm", "type": "categorical"})
    with pytest.raises(ValueError, match="categories"):
        _fields(m, {"name": "Arm", "type": "text", "categories": ["a"]})
    with pytest.raises(ValueError, match="categories"):
        _fields(m, {"name": "Arm", "type": "categorical", "categories": ["a"] * 51})
    with pytest.raises(ValueError, match="type"):
        _fields(m, {"name": "Arm", "type": "date"})
    with pytest.raises(ValueError, match="unique"):
        _fields(m, {"name": "Arm"}, {"name": "Arm"})
    with pytest.raises(ValueError, match="unit"):
        _fields(m, {"name": "Arm", "unit": "x" * 51})


def test_form_hash_ignores_nothing_but_order_matters() -> None:
    m, protocol = uuid4(), uuid4()
    a, b = {"name": "A"}, {"name": "B"}
    ab, ba = _fields(m, a, b), _fields(m, b, a)
    assert rules.form_hash("authored", protocol, ab) == rules.form_hash(
        "authored", protocol, _fields(m, a, b)
    )
    assert rules.form_hash("authored", protocol, ab) != rules.form_hash(
        "authored", protocol, ba
    )
    assert rules.form_hash("authored", protocol, ab) != rules.form_hash(
        "authored", None, ab
    )
    assert rules.form_hash("authored", None, ab) != rules.form_hash(
        "legacy_unversioned", None, ab
    )
    described = _fields(m, {"name": "A", "description": "x"}, b)
    assert rules.form_hash("authored", None, ab) != rules.form_hash(
        "authored", None, described
    )


def test_coerce_number_boolean_categorical_text() -> None:
    m = uuid4()
    number, boolean, text, cat = _fields(
        m,
        {"name": "N", "type": "number"},
        {"name": "B", "type": "boolean"},
        {"name": "T"},
        {"name": "C", "type": "categorical", "categories": ["RCT", "Cohort"]},
    )
    assert rules.coerce(number, "12") == (12, True)
    assert rules.coerce(number, " 12.5 ") == (12.5, True)
    assert rules.coerce(number, 12.0) == (12, True)
    assert rules.coerce(number, "12 participants") == ("12 participants", False)
    assert rules.coerce(number, True) == ("true", False)
    assert rules.coerce(number, "nan") == ("nan", False)
    assert rules.coerce(boolean, "Yes") == (True, True)
    assert rules.coerce(boolean, "no") == (False, True)
    assert rules.coerce(boolean, False) == (False, True)
    assert rules.coerce(boolean, "maybe") == ("maybe", False)
    assert rules.coerce(cat, "rct") == ("RCT", True)
    assert rules.coerce(cat, "RCTs") == ("RCTs", False)
    assert rules.coerce(text, "anything") == ("anything", True)
    assert rules.coerce(text, "  ") == ("  ", False)
    assert rules.coerce(text, {"nested": 1}) == ('{"nested": 1}', False)


def test_missingness_taxonomy_per_writer() -> None:
    assert len(rules.MISSINGNESS_VALUES) == 5
    rules.check_missingness("machine", "extraction_error")
    rules.check_missingness("human", "not_reported")
    rules.check_missingness("accepted", "unresolved_disagreement")
    for writer, reason in (
        ("machine", "unresolved_disagreement"),
        ("human", "extraction_error"),
        ("human", "unresolved_disagreement"),
        ("accepted", "extraction_error"),
        ("human", "made_up"),
    ):
        with pytest.raises(ValueError):
            rules.check_missingness(writer, reason)


def test_value_xor_missingness() -> None:
    (number,) = _fields(uuid4(), {"name": "N", "type": "number"})
    with pytest.raises(ValueError, match="exactly one"):
        rules.normalize("human", number, None, None)
    with pytest.raises(ValueError, match="exactly one"):
        rules.normalize("human", number, "12", "not_reported")
    assert rules.normalize("human", number, "12", None) == (12, None, "valid")
    assert rules.normalize("human", number, None, "not_reported") == (
        None,
        "not_reported",
        "valid",
    )
    # A machine value that fails coercion is kept; a human one is rejected.
    assert rules.normalize("machine", number, "12 people", None) == (
        "12 people",
        None,
        "invalid",
    )
    with pytest.raises(ValueError, match="field type"):
        rules.normalize("human", number, "12 people", None)


def test_display() -> None:
    assert rules.display(12.0) == "12"
    assert rules.display(12.5) == "12.5"
    assert rules.display(True) == "true"
    assert rules.display("RCT") == "RCT"
    assert rules.display(None) is None


def test_stale_on_field_def_change_not_on_unrelated_column() -> None:
    m = uuid4()
    v2 = _fields(m, {"name": "N", "type": "number"}, {"name": "D"})
    v3 = _fields(m, {"name": "N", "type": "number"}, {"name": "D"}, {"name": "X"})
    v4 = _fields(m, {"name": "N", "type": "number", "unit": "participants"})
    fid = rules.field_id(m, "N")
    old = rules.field_def(v2, fid)
    assert old is not None and set(old) == {"type", "unit", "timepoint", "categories"}
    assert not rules.is_stale(old, rules.field_def(v3, fid), H1, H1)
    assert rules.is_stale(old, rules.field_def(v4, fid), H1, H1)
    removed = _fields(m, {"name": "D"})
    assert rules.field_def(removed, fid) is None
    assert rules.is_stale(old, None, H1, H1)
    # A description edit is not a definition change.
    described = _fields(m, {"name": "N", "type": "number", "description": "n"})
    assert not rules.is_stale(old, rules.field_def(described, fid), H1, H1)


def test_stale_on_source_hash_change() -> None:
    (field,) = _fields(uuid4(), {"name": "N"})
    definition = rules.field_def([field], UUID(field["field_id"]))
    assert rules.is_stale(definition, definition, H1, H2)
    assert not rules.is_stale(definition, definition, H1, H1)


def _accepted(
    value: object, definition: dict | None, source_hash: str = H1
) -> Accepted:
    return Accepted(
        id=uuid4(),
        value=value,
        missingness=None,
        form_version_id=uuid4(),
        source_hash=source_hash,
        field_def=definition,
    )


def test_pick_cell_precedence_accepted_then_machine_then_legacy() -> None:
    (field,) = _fields(uuid4(), {"name": "N", "type": "number"})
    definition = rules.field_def([field], UUID(field["field_id"]))
    legacy = LegacyCell(
        value="twelve", citation_snippet="p3", confidence=0.8, form_version_id=None
    )
    machine = [_obs(10), _obs(None, "not_reported")]
    accepted = [_accepted(12, definition)]

    cell = rules.pick_cell(accepted, machine, legacy, definition, H1)
    assert cell is not None
    assert (cell.source, cell.value, cell.stale, cell.confidence) == (
        "accepted",
        "12",
        False,
        None,
    )

    cell = rules.pick_cell([], machine, legacy, definition, H1)
    assert cell is not None
    assert (cell.source, cell.value, cell.missingness) == (
        "machine",
        None,
        "not_reported",
    )

    cell = rules.pick_cell([], [], legacy, definition, H1)
    assert cell is not None
    assert (cell.source, cell.value, cell.confidence, cell.stale) == (
        "legacy",
        "twelve",
        0.8,
        False,
    )
    assert cell.validation_state is None

    assert rules.pick_cell([], [], None, definition, H1) is None


def test_pick_cell_prefers_non_stale_accepted() -> None:
    m = uuid4()
    (field,) = _fields(m, {"name": "N", "type": "number"})
    definition = rules.field_def([field], UUID(field["field_id"]))
    (older,) = _fields(m, {"name": "N"})
    stale = _accepted(11, rules.field_def([older], UUID(older["field_id"])))
    fresh = _accepted(12, definition)
    cell = rules.pick_cell([stale, fresh], [], None, definition, H1)
    assert cell is not None and (cell.value, cell.stale) == ("12", False)
    # A stale tip is still shown as accepted, marked stale.
    cell = rules.pick_cell([stale], [_obs(9)], None, definition, H1)
    assert cell is not None and (cell.source, cell.stale) == ("accepted", True)
    cell = rules.pick_cell([fresh], [], None, definition, H2)
    assert cell is not None and cell.stale


def test_acceptance_must_equal_a_valid_cited_observation() -> None:
    twelve, invalid = _obs(12), _obs("12 people", state="invalid")
    rules.check_acceptance(12, None, [twelve, invalid])
    with pytest.raises(ValueError, match="cited"):
        rules.check_acceptance(13, None, [twelve])
    with pytest.raises(ValueError, match="cited"):
        rules.check_acceptance("12 people", None, [twelve, invalid])
    # Type matters: True is not 1.
    with pytest.raises(ValueError, match="cited"):
        rules.check_acceptance(True, None, [_obs(1)])
    missing = _obs(None, "not_reported", kind="human")
    rules.check_acceptance(None, "not_reported", [missing])
    with pytest.raises(ValueError, match="cited"):
        rules.check_acceptance(None, "not_applicable", [missing])
    with pytest.raises(ValueError):
        rules.check_acceptance(None, None, [twelve])
    with pytest.raises(ValueError):
        rules.check_acceptance(12, None, [])


def test_unresolved_disagreement_needs_two_differing() -> None:
    a, b, a2 = _obs(12), _obs(14), _obs(12)
    rules.check_acceptance(None, "unresolved_disagreement", [a, b])
    rules.check_acceptance(
        None, "unresolved_disagreement", [a, _obs(None, "not_reported")]
    )
    with pytest.raises(ValueError, match="two"):
        rules.check_acceptance(None, "unresolved_disagreement", [a])
    with pytest.raises(ValueError, match="differ"):
        rules.check_acceptance(None, "unresolved_disagreement", [a, a2])


def test_migration_copies_match_the_rules() -> None:
    """a3c5e7f9b1d4 froze copies of FIELD_NAMESPACE and the form hash."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).parents[3]
        / "alembic/versions/a3c5e7f9b1d4_version_extraction_forms.py"
    )
    spec = importlib.util.spec_from_file_location("goo304_migration", path)
    assert spec is not None and spec.loader is not None
    migration: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration._FIELD_NAMESPACE == rules.FIELD_NAMESPACE
    matrix_id = uuid4()
    columns = [{"name": "Sample size", "description": "n"}, {"name": "Design"}]
    legacy = migration._legacy_fields(matrix_id, columns, ["Old col"])
    assert legacy == rules.build_fields(matrix_id, [*columns, {"name": "Old col"}])
    assert migration._content_hash(legacy) == rules.form_hash(
        "legacy_unversioned", None, legacy
    )
