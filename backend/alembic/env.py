"""
Alembic environment configuration
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add the src directory to Python path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

# Import all models to ensure they are registered with Base.metadata
# The __init__.py exports all SQLAlchemy models
from src.models import (
    AnalyticsEvent,
    AuditEvent,
    ChatMessage,
    Citation,
    Collection,
    CollectionDocument,
    ComplianceReport,
    Conversation,
    DataRetentionPolicy,
    Document,
    DocumentAccessLog,
    DocumentQualityMetrics,
    DocumentVersion,
    EncryptedOrganizationProfile,
    EncryptedUserProfile,
    EncryptionAuditLog,
    Entity,
    MessageAttachment,
    MultimodalContent,
    Organization,
    PerformanceLog,
    Permission,
    ProcessingHistory,
    ProcessingJob,
    QualityMetric,
    Role,
    SearchQuery,
    SearchResult,
    SearchSession,
    SecurityIncident,
    Thread,
    User,
    UserRoleAssignment,
    UserSession,
    Workspace,
    WorkspaceMember,
)

# Import models and database configuration
from src.models.base import Base
from src.models.document_embedding import MIGRATION_OWNED_TABLES

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Leave migration-owned tables (not on Base.metadata) out of autogenerate.

    ``document_embeddings`` needs the pgvector extension, so only
    ``jd01_document_embeddings`` creates it; without this the reflected table
    would be reported as "removed" by ``alembic check``.
    """
    return not (type_ == "table" and name in MIGRATION_OWNED_TABLES)


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def _install_idempotent_op_guards() -> None:
    """R6-M9: make the historical chain tolerant of both provisioning worlds.

    Live databases were provisioned by ``Base.metadata.create_all`` while the
    chain was only ever run piecemeal; since r6h3_model_baseline a fresh
    migrate-only database also reaches every revision with complete model
    tables. Historical revisions therefore routinely meet objects that
    already exist. Rather than hand-guarding 60 files, make the alembic
    op.* DDL helpers idempotent here — logged loudly so genuine authoring
    mistakes stay visible.
    """
    import logging

    import sqlalchemy as sa
    from alembic import op as _op

    log = logging.getLogger("alembic.runtime.guards")

    def _inspector():
        return sa.inspect(_op.get_bind())

    _orig_create_table = _op.create_table

    def create_table(name, *args, **kwargs):
        if _inspector().has_table(name):
            log.warning("create_table skipped, %s already exists", name)
            return None
        return _orig_create_table(name, *args, **kwargs)

    _orig_add_column = _op.add_column

    def add_column(table_name, column, *args, **kwargs):
        if not _inspector().has_table(table_name):
            log.warning("add_column skipped, table %s missing", table_name)
            return None
        cols = {c["name"] for c in _inspector().get_columns(table_name)}
        if column.name in cols:
            log.warning(
                "add_column skipped, %s.%s already exists", table_name, column.name
            )
            return None
        return _orig_add_column(table_name, column, *args, **kwargs)

    _orig_drop_column = _op.drop_column

    def drop_column(table_name, column_name, *args, **kwargs):
        if table_name not in {t for t in _inspector().get_table_names()}:
            log.warning("drop_column skipped, table %s missing", table_name)
            return None
        cols = {c["name"] for c in _inspector().get_columns(table_name)}
        if column_name not in cols:
            log.warning("drop_column skipped, %s.%s missing", table_name, column_name)
            return None
        return _orig_drop_column(table_name, column_name, *args, **kwargs)

    _orig_create_index = _op.create_index

    def create_index(index_name, table_name, *args, **kwargs):
        # R6-M9: some historical revisions index tables that neither the
        # models nor any revision create anymore (retired features) —
        # UndefinedTable on those is expected on every provisioning path.
        if table_name not in set(_inspector().get_table_names()):
            log.warning("create_index skipped, table %s does not exist", table_name)
            return None
        try:
            idx = {i["name"] for i in _inspector().get_indexes(table_name)}
        except Exception:
            idx = set()
        if index_name in idx:
            log.warning("create_index skipped, %s exists", index_name)
            return None
        return _orig_create_index(index_name, table_name, *args, **kwargs)

    _orig_drop_index = _op.drop_index

    def drop_index(index_name, table_name=None, *args, **kwargs):
        # Index names are schema-global relations in Postgres, so one
        # to_regclass lookup covers every case the inspector could not:
        # table_name omitted, or the owning table itself missing.
        exists = (
            _op.get_bind()
            .execute(
                sa.text("SELECT to_regclass(:n) IS NOT NULL"),
                {"n": index_name},
            )
            .scalar()
        )
        if not exists:
            log.warning("drop_index skipped, %s missing", index_name)
            return None
        return _orig_drop_index(index_name, table_name, *args, **kwargs)

    _orig_create_table_hooked = create_table
    _op.create_table = create_table
    _op.add_column = add_column
    _op.drop_column = drop_column
    _op.create_index = create_index
    _op.drop_index = drop_index

    def _constraint_exists(name: str, table: str | None = None) -> bool:
        # Constraint names are only unique per table in Postgres, so scope the
        # lookup by conrelid whenever the caller knows the table — otherwise a
        # same-named constraint elsewhere makes the guard skip (or drop) the
        # wrong object.
        if table:
            sql = sa.text(
                "SELECT 1 FROM pg_constraint c "
                "JOIN pg_class r ON r.oid = c.conrelid AND r.relname = :t "
                "WHERE c.conname = :n"
            )
            params = {"n": name, "t": table}
        else:
            sql = sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n")
            params = {"n": name}
        row = _op.get_bind().execute(sql, params).scalar()
        if row:
            return True
        # Unique/PK constraints materialize a same-named index relation; some
        # revisions created plain indexes under constraint names.
        return (
            _op.get_bind()
            .execute(
                sa.text("SELECT to_regclass(:r) IS NOT NULL"),
                {"r": name},
            )
            .scalar()
        )

    def _skip_constraint(name: str, kind: str, table: str | None = None) -> bool:
        if _constraint_exists(name, table):
            log.warning("%s skipped, %s already exists", kind, name)
            return True
        return False

    _orig_create_unique = _op.create_unique_constraint
    _op.create_unique_constraint = lambda name, source, cols, **kw: (
        None
        if _skip_constraint(name, "create_unique_constraint", source)
        else _orig_create_unique(name, source, cols, **kw)
    )

    _orig_create_pk = _op.create_primary_key
    _op.create_primary_key = lambda name, source, cols, **kw: (
        None
        if _skip_constraint(name, "create_primary_key", source)
        else _orig_create_pk(name, source, cols, **kw)
    )

    _orig_create_fk = _op.create_foreign_key

    def create_foreign_key(name, source, referent, local_cols, remote_cols, **kw):
        if name and _skip_constraint(name, "create_foreign_key", source):
            return None
        return _orig_create_fk(name, source, referent, local_cols, remote_cols, **kw)

    _op.create_foreign_key = create_foreign_key

    _orig_create_check = _op.create_check_constraint
    _op.create_check_constraint = lambda name, source, condition, **kw: (
        None
        if _skip_constraint(name, "create_check_constraint", source)
        else _orig_create_check(name, source, condition, **kw)
    )

    _orig_drop_constraint = _op.drop_constraint

    def drop_constraint(name, table_name, type_=None):
        if not _constraint_exists(name, table_name):
            log.warning("drop_constraint skipped, %s missing on %s", name, table_name)
            return None
        return _orig_drop_constraint(name, table_name, type_) or None

    _op.drop_constraint = drop_constraint


def get_url():
    """Get database URL from environment."""
    url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if url:
        return url
    try:
        from src.core.config import settings

        return settings.DATABASE_URL
    except Exception:
        return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.
    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = get_url()

    _install_idempotent_op_guards()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
