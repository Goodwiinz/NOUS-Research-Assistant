# scripts/

Repository scripts. Classify one by reading it, not by its filename: some are
read-only checks and some touch backups, databases, deployments or shared
environments.

| Path | Contents |
| --- | --- |
| `ci/` | Blocking CI helpers: `run_local_ci.sh`, `generate_openapi.py`, `check_alembic.py`, `select_ci_jobs.py`, `assert_required_jobs.py`, frontend quality checks. Used by `.github/workflows/test-pipeline.yml`. |
| `docs/` | Directory-doc lint (`check_dir_docs.py`); see [`docs/README.md`](docs/README.md). |
| `nous/` | Python modules for the NOUS coordination tooling (`preflight.py` runtime config, `coordination.py` typed claim contracts, backend and repository helpers). |
| `audit/`, `perf/`, `verify/` | Audit, benchmark and verification helpers. |
| `backup/`, `maintenance/`, `startup/` | Operational scripts: database backup and recovery, knowledge-graph maintenance, local service start scripts. |
| `data/`, `testing/`, `utilities/` | Ad hoc data, integration-test and diagnostic scripts, many from earlier development phases. |
| top-level `*.sh` / `*.py` | One-off deployment, setup, health-check and arXiv/knowledge-graph scripts. Several are historical; check callers before relying on one. |

Scripts that act on shared environments (backups, deploys, load tests) need
explicit authorization before running. Editing rules are in
[`AGENTS.md`](AGENTS.md); the CI job map is in
[`docs/engineering/testing.md`](../docs/engineering/testing.md).
