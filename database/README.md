# database/

Index of standalone database assets. None of these are the live schema
authority: backend schema changes go through Alembic in `backend/alembic/`, and
the Supabase CLI history lives in [`supabase/`](../supabase/README.md). Check
[`docs/engineering/backend.md`](../docs/engineering/backend.md) and
[`AGENTS.md`](AGENTS.md) before running any of this against a shared database.

## Subfolders

| Path | Contents |
| --- | --- |
| `init/` | Early bootstrap: `01_schema.sql`, Neo4j and Redis setup, a Qdrant setup file (Qdrant has since been removed), `05_database_init.sh`. |
| `ci-init/` | `00_auth_schema.sql`, referenced by the CI Compose file (`config/docker-compose/docker-compose.ci.yml`) as an init-script mount. |
| `migrations/` | Numbered standalone SQL migrations (A/B testing, monitoring, analytics, realtime status, document Q&A). Not the Alembic chain. |
| `deployment/`, `maintenance/`, `indexing/`, `optimization/`, `procedures/`, `queries/`, `monitoring/`, `triggers/`, `views/`, `security/` | SQL for deploying, indexing, tuning and querying the analytics and realtime-status features. |

## Loose files

- SQL and config: `ab_testing_schema.sql`, `monitoring_schema.sql`,
  `init_knowledge_graph_db.sql`, `indexing_strategy.sql`,
  `partitioning_strategy.sql`, `performance_optimization.sql`,
  `data_retention_policies.sql`, `neo4j-init.cypher`,
  `neo4j_performance.cypher`, `redis_knowledge_graph_config.lua`,
  `redis_optimization.conf`, `qdrant_knowledge_graph_collections.json`,
  `health_checks.py`.
- Guides: `README_AB_Testing_Implementation.md`,
  `ab_testing_performance_guide.md`, `MONITORING_IMPLEMENTATION_GUIDE.md`,
  `monitoring_erd.md`.

## Cleanup candidates (tracked, not deleted)

These look like stray local exports rather than project assets and are
candidates for removal in a deliberate change:

- `database/export (2).csv`
- `database/records (4).json`
- `supabase/.branches/_current_branch` (Supabase CLI local state)
