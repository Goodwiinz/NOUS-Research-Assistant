"""Contract tests for the PostgREST product-table lockdown migration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


ROOT = Path(__file__).parents[3]
HARDEN_RLS = ROOT / "supabase/migrations/20260305001600_harden_rls_policies.sql"
LOCKDOWN = (
    ROOT
    / "supabase/migrations/20260917000000_revoke_product_tables_data_api_access.sql"
)


def _authenticated_read_only_tables(sql: str) -> set[str]:
    """Derive tables covered by the old authenticated read policy."""
    tables: set[str] = set()
    for match in re.finditer(
        r"tables\s+TEXT\[\]\s*:=\s*ARRAY\[(.*?)\];", sql, re.S | re.I
    ):
        block = match.group(1)
        end = sql.find("$$", match.end())
        procedure = sql[match.start() : end if end >= 0 else len(sql)]
        if "authenticated_read_only" in procedure:
            tables.update(re.findall(r"'([a-z][a-z0-9_]*)'", block))
    return tables


def test_every_authenticated_read_only_product_table_is_revoked() -> None:
    """The follow-up migration must cover every table opened by the old one."""
    old_tables = _authenticated_read_only_tables(HARDEN_RLS.read_text())
    assert old_tables, "the source RLS migration yielded no product tables"
    assert LOCKDOWN.exists(), "the product-table lockdown migration is missing"

    lockdown_sql = LOCKDOWN.read_text()
    quoted_identifiers = set(re.findall(r"'([a-z][a-z0-9_]*)'", lockdown_sql))

    assert old_tables <= quoted_identifiers
    assert 'DROP POLICY IF EXISTS "authenticated_read_only"' in lockdown_sql
    assert "REVOKE ALL ON TABLE public.%I FROM anon, authenticated" in lockdown_sql
    assert "ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY" in lockdown_sql
    assert "to_regclass" in lockdown_sql
