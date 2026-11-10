"""secure agent_memories and search_analytics behind RLS

``h8i9j0k1l2m3`` locked down the backend-only agent tables but listed eight
tables exactly; ``agent_memories`` and ``search_analytics`` are created by
this Alembic chain too and were left out (audit A4-RLS1). Both are backend
tables reached over the direct postgres connection, never from the browser.

Same boundary as ``h8i9j0k1l2m3``: enable RLS with no client policies and
revoke the two PostgREST roles. Unlike that revision, a missing table is
skipped with a NOTICE: live dev never created either table (they are absent
there today), and a security migration must not block the upgrade chain on
a table that does not exist. When a table appears later the Supabase
default-privilege revocation (20260929084430) still keeps PostgREST out.

Revision ID: rl01_secure_late_internal_rls
Revises: cd01_created_at_default_now
Create Date: 2026-11-10
"""

from alembic import op  # type: ignore[attr-defined]

revision = "rl01_secure_late_internal_rls"
down_revision = "cd01_created_at_default_now"
branch_labels = None
depends_on = None

_TABLES = ("agent_memories", "search_analytics")

_POSTGREST_ROLES = ("anon", "authenticated")


def upgrade() -> None:
    for table in _TABLES:
        revokes = "\n".join(f"""
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        EXECUTE 'REVOKE ALL ON TABLE public."{table}" FROM {role}';
                    END IF;""" for role in _POSTGREST_ROLES)
        op.execute(f"""
            DO $$
            BEGIN
                IF to_regclass('public."{table}"') IS NULL THEN
                    RAISE NOTICE 'skipping RLS for missing table public.{table}';
                    RETURN;
                END IF;
                EXECUTE 'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY';{revokes}
            END
            $$
            """)


def downgrade() -> None:
    # Intentionally preserve the security boundary on downgrade. Regranting
    # PostgREST access or disabling RLS would recreate the data exposure.
    pass
