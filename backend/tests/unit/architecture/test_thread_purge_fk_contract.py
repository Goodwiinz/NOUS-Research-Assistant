"""Every foreign key a thread hard-delete reaches says what happens to its row.

``purge_soft_deleted_threads`` (src/tasks/retention_tasks.py) deletes a
chat's ``chat_messages`` and then the thread with bulk DELETEs: no ORM cascade
runs, so the database's ON DELETE rule is all that clears a referencing row. A
foreign key with no rule (NO ACTION) into any table that delete reaches makes
PostgreSQL refuse it. Seven thread FKs added 09-28..10-05 plus
``artifact_references.message_id`` did exactly that and stopped the purge for
every organization (audit HO-2, 2026-10-08).

Start from the two tables the purge deletes, follow every CASCADE to the
tables it deletes from in turn, and require CASCADE or SET NULL on every FK
into any of them. A new table that names a chat fails here until it picks
one. This reads the model metadata; the rp01 migration and
tests/integration/test_retention_thread_purge_postgres.py hold the live
database to the same rules.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base

pytestmark = pytest.mark.unit

# What purge_soft_deleted_threads deletes itself.
PURGED = frozenset({"threads", "chat_messages"})
RULES = frozenset({"CASCADE", "SET NULL"})


def _walk(metadata: MetaData = Base.metadata) -> tuple[set[str], list[str]]:
    """Tables a thread delete reaches, and every FK into them with no rule
    PostgreSQL can carry out."""
    reached = set(PURGED)
    unruled: set[str] = set()
    grew = True
    while grew:
        grew = False
        for table in metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column.table.name not in reached:
                    continue
                rule = (fk.ondelete or "NO ACTION").upper()
                if rule not in RULES:
                    unruled.add(
                        f"{table.name}.{fk.parent.name} -> {fk.target_fullname}"
                    )
                elif rule == "SET NULL" and not fk.parent.nullable:
                    # PostgreSQL refuses the delete with a not-null violation,
                    # which wedges the purge just as NO ACTION does.
                    unruled.add(
                        f"{table.name}.{fk.parent.name} -> {fk.target_fullname}"
                        " (SET NULL on NOT NULL)"
                    )
                elif rule == "CASCADE" and table.name not in reached:
                    reached.add(table.name)
                    grew = True
    return reached, sorted(unruled)


def test_every_fk_a_thread_delete_reaches_has_an_on_delete_rule() -> None:
    _, unruled = _walk()
    assert unruled == []


def test_the_walk_follows_cascades_into_the_consent_tables() -> None:
    # Guards the guard: a walk that stopped at threads would never check the
    # FKs into the consent a chat-bound grant request cascades to.
    reached, _ = _walk()
    assert {
        "integration_handoffs",
        "integration_grant_requests",
        "integration_context_selections",
    } <= reached


def test_the_walk_rejects_set_null_on_a_not_null_column() -> None:
    # Guards the guard: a non-Optional Mapped[...] column is NOT NULL, so
    # ondelete="SET NULL" on it passes a rule-name check and still wedges the
    # purge. A nullable SET NULL column is fine and must not be reported.
    metadata = MetaData()
    Table("threads", metadata, Column("id", Integer, primary_key=True))
    Table("chat_messages", metadata, Column("id", Integer, primary_key=True))
    Table(
        "notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "thread_id",
            ForeignKey("threads.id", ondelete="SET NULL"),
            nullable=False,
        ),
        Column("message_id", ForeignKey("chat_messages.id", ondelete="SET NULL")),
    )
    _, unruled = _walk(metadata)
    assert unruled == ["notes.thread_id -> threads.id (SET NULL on NOT NULL)"]
