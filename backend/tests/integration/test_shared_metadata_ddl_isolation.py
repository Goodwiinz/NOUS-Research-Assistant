"""A full-metadata ``create_all`` must not strip foreign keys from later tests.

On PostgreSQL, ``Base.metadata.create_all`` breaks foreign-key cycles by
emitting those constraints with ``AddConstraint``. That marks the shared
``ForeignKeyConstraint`` objects so every later ``CREATE TABLE`` in the process
leaves them out. A bounded schema built afterwards with ``Table.create()``
then has no foreign keys. ``test_agent_run_concurrency.py`` failed in CI that
way after the screening fixtures ran (the dangling-thread insert succeeded).
The integration conftest restores the constraints after every test. These two
tests run in file order and pin both halves.
"""

import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from src.models.agent_run import AgentRun
from src.models.base import Base

pytestmark = pytest.mark.integration


def _agent_run_create_table() -> str:
    return str(CreateTable(AgentRun.__table__).compile(dialect=postgresql.dialect()))


def test_postgres_create_all_hides_cyclic_foreign_keys_until_teardown() -> None:
    assert "REFERENCES threads" in _agent_run_create_table()

    engine = create_mock_engine("postgresql://", lambda *_args, **_kwargs: None)
    Base.metadata.create_all(engine, checkfirst=False)

    # SQLAlchemy behavior, not ours. If this stops holding, the conftest
    # restore fixture is no longer needed.
    assert "REFERENCES threads" not in _agent_run_create_table()


def test_next_test_sees_the_foreign_keys_again() -> None:
    assert "REFERENCES threads" in _agent_run_create_table()
