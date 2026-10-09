"""Q-P3: ``GET /api/v2/search/health`` must recognise PostgreSQL's GIN indexes.

``pg_indexes.indexdef`` renders the access method in lower case
(``... USING gin (search_vector)``), so the former ``indexdef LIKE '%GIN%'``
matched nothing and the endpoint reported ``index_count: 0`` on a healthy
database. An INVALID index (an interrupted ``CONCURRENTLY`` build) serves no
query, so it must not count either. The fake session below answers the
handler's own SQL the way PostgreSQL would: ``LIKE`` is case-sensitive,
``ILIKE`` is not, and a predicate on ``indisvalid`` drops invalid rows.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, List, Tuple, cast

import pytest

from src.api.threads.thread_search import search_health_check
from src.models import User

pytestmark = pytest.mark.unit

# (indexdef, indisvalid), verbatim from a PostgreSQL 14 database after
# b2c3d4e5f6g7 plus one interrupted CONCURRENTLY build.
INDEXES: dict[str, Tuple[str, bool]] = {
    "idx_threads_search_vector": (
        "CREATE INDEX idx_threads_search_vector ON public.threads "
        "USING gin (search_vector)",
        True,
    ),
    "idx_threads_title_gin": (
        "CREATE INDEX idx_threads_title_gin ON public.threads USING gin "
        "(to_tsvector('english'::regconfig, (COALESCE(title, ''::character "
        "varying))::text))",
        True,
    ),
    "idx_chat_messages_search_vector": (
        "CREATE INDEX idx_chat_messages_search_vector ON public.chat_messages "
        "USING gin (search_vector)",
        True,
    ),
    "idx_chat_messages_content_gin": (
        "CREATE INDEX idx_chat_messages_content_gin ON public.chat_messages "
        "USING gin (to_tsvector('english'::regconfig, COALESCE(content, ''::text)))",
        False,
    ),
    "idx_chat_messages_thread_role": (
        "CREATE INDEX idx_chat_messages_thread_role ON public.chat_messages "
        "USING btree (thread_id, role)",
        True,
    ),
    "threads_pkey": (
        "CREATE UNIQUE INDEX threads_pkey ON public.threads USING btree (id)",
        True,
    ),
}
VALID_GIN = {
    "idx_threads_search_vector",
    "idx_threads_title_gin",
    "idx_chat_messages_search_vector",
}

_PREDICATE = re.compile(r"indexdef\s+(ILIKE|LIKE)\s+'([^']*)'", re.IGNORECASE)


def _like(pattern: str, value: str, *, case_insensitive: bool) -> bool:
    regex = ".*".join(re.escape(part) for part in pattern.split("%"))
    flags = re.IGNORECASE if case_insensitive else 0
    return re.fullmatch(regex, value, flags | re.DOTALL) is not None


class _PgIndexesSession:
    """Answers the health handler's two statements like PostgreSQL."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, clause: Any) -> Any:
        sql = str(clause)
        self.statements.append(sql)
        if "plainto_tsquery" in sql:
            return [(0,)]
        match = _PREDICATE.search(sql)
        assert match, f"unexpected health SQL: {sql}"
        operator, pattern = match.group(1).upper(), match.group(2)
        requires_valid = "indisvalid" in sql
        return [
            SimpleNamespace(indexname=name, indexdef=definition)
            for name, (definition, valid) in INDEXES.items()
            if _like(pattern, definition, case_insensitive=operator == "ILIKE")
            and (valid or not requires_valid)
        ]


def test_health_counts_lower_case_using_gin_definitions() -> None:
    db = _PgIndexesSession()

    body = search_health_check(db=cast(Any, db), current_user=cast(User, None))

    assert body["search_functional"] is True
    assert body["status"] == "healthy"
    assert {index["name"] for index in body["gin_indexes"]} == VALID_GIN
    assert body["index_count"] == len(VALID_GIN)
    assert all(index["gin"] is True for index in body["gin_indexes"])


def test_health_ignores_btree_and_invalid_indexes() -> None:
    body = search_health_check(
        db=cast(Any, _PgIndexesSession()), current_user=cast(User, None)
    )

    names = {index["name"] for index in body["gin_indexes"]}
    assert "idx_chat_messages_thread_role" not in names
    assert "threads_pkey" not in names
    assert "idx_chat_messages_content_gin" not in names  # INVALID
