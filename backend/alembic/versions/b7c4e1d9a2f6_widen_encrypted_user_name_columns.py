"""Widen users.first_name / users.last_name to TEXT for encrypted values

Revision ID: b7c4e1d9a2f6
Revises: merge_daily_harness_20260928
Create Date: 2026-09-29 00:00:00.000000

PR #175 (bded25e35) switched ``User.first_name`` / ``User.last_name`` to
``encrypted_string(...)``, whose column impl is ``TEXT``, but shipped no
migration. The live columns stayed ``VARCHAR(100)``. The stored ciphertext is a
JSON envelope (``{"encrypted_data": ..., "nonce": ..., "version": ...}``) of
several hundred characters, so every ``users`` INSERT fails with
``value too long for type character varying(100)`` once
``ENCRYPTION_MASTER_KEY`` is set, which breaks JIT provisioning of new users.

The materialized view ``public.realtime_document_dashboard`` selects both
columns, so PostgreSQL refuses ``ALTER COLUMN ... TYPE`` while it exists
("cannot alter type of a column used by a view or rule"). ``upgrade()`` drops
it, alters the columns, and recreates it under the same name (it is refreshed
by name in ``refresh_realtime_views()``). It is recreated ``WITH NO DATA``
and without indexes, matching the live database (unpopulated, no indexes;
the indexes were dropped in supabase/migrations/20260305002500), and the
browser-role SELECT revoke from supabase/migrations/20260304234900 is
re-applied because a recreated relation loses its grants. On a fresh
migrate-only database the view (and ``document_processing_stages``, which it
reads) does not exist, so it is only dropped and recreated when it was present.
"""

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision = "b7c4e1d9a2f6"
down_revision = "merge_daily_harness_20260928"
branch_labels = None
depends_on = None

# Verbatim from the live database (pg_get_viewdef of the deployed view).
_REALTIME_DOCUMENT_DASHBOARD_SELECT = """
 SELECT d.id,
    d.title,
    d.filename,
    d.document_type AS file_type,
    d.processing_status AS status,
    d.current_processing_stage,
    d.processing_progress,
    d.queue_priority,
    d.worker_assignment_id,
    d.last_status_update,
    d.created_at AS uploaded_at,
    d.processing_started_at,
    u.email AS uploaded_by_email,
    (u.first_name::text || ' '::text) || COALESCE(u.last_name, ''::character varying)::text AS uploaded_by_name,
    count(DISTINCT ps.id) AS total_stages,
    count(
        CASE
            WHEN ps.status::text = 'completed'::text THEN 1
            ELSE NULL::integer
        END) AS completed_stages,
    count(
        CASE
            WHEN ps.status::text = 'running'::text THEN 1
            ELSE NULL::integer
        END) AS running_stages,
    count(
        CASE
            WHEN ps.status::text = 'failed'::text THEN 1
            ELSE NULL::integer
        END) AS failed_stages,
        CASE
            WHEN d.processing_status = 'PROCESSING'::processingstatus THEN
            CASE
                WHEN count(ps.id) = 0 THEN 0::numeric
                ELSE round(count(
                CASE
                    WHEN ps.status::text = 'completed'::text THEN 1
                    ELSE NULL::integer
                END)::numeric * 100.0 / count(ps.id)::numeric, 2)
            END
            WHEN d.processing_status = 'COMPLETED'::processingstatus THEN 100::numeric
            WHEN d.processing_status = 'FAILED'::processingstatus THEN 0::numeric
            ELSE d.processing_progress
        END AS calculated_progress,
        CASE
            WHEN d.processing_started_at IS NOT NULL THEN EXTRACT(epoch FROM now() - d.processing_started_at)::integer
            ELSE NULL::integer
        END AS processing_duration_seconds
   FROM documents d
     JOIN users u ON d.uploaded_by_user_id = u.id
     LEFT JOIN document_processing_stages ps ON d.id = ps.document_id
  WHERE d.is_deleted = false
  GROUP BY d.id, d.title, d.filename, d.document_type, d.processing_status, d.current_processing_stage, d.processing_progress, d.queue_priority, d.worker_assignment_id, d.last_status_update, d.created_at, d.processing_started_at, u.email, u.first_name, u.last_name
"""


def upgrade() -> None:
    if context.is_offline_mode():
        # No connection to introspect offline (--sql); render the full path.
        had_view = True
    else:
        had_view = bool(
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT to_regclass('public.realtime_document_dashboard') IS NOT NULL"
                )
            )
            .scalar()
        )
    if had_view:
        op.execute("DROP MATERIALIZED VIEW public.realtime_document_dashboard")
    for column in ("first_name", "last_name"):
        op.alter_column(
            "users",
            column,
            type_=sa.Text(),
            existing_type=sa.String(length=100),
            existing_nullable=False,
        )
    if not had_view:
        return
    op.execute(
        "CREATE MATERIALIZED VIEW public.realtime_document_dashboard AS"
        + _REALTIME_DOCUMENT_DASHBOARD_SELECT
        + "WITH NO DATA"
    )
    # A recreated relation loses its grants; keep it closed to Supabase browser
    # roles (they do not exist on every environment, hence the guard).
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON public.realtime_document_dashboard FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON public.realtime_document_dashboard FROM authenticated;
            END IF;
        END
        $$
        """)


def downgrade() -> None:
    # Encrypted values exceed VARCHAR(100); narrowing would fail or truncate
    # ciphertext and make the rows undecryptable.
    raise RuntimeError("Irreversible: encrypted values exceed VARCHAR(100)")
