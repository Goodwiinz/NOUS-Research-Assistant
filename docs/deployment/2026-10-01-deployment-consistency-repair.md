# Deployment consistency repair — 2026-10-01

Status: code prepared for review; application rollout not performed.
Base source: `057681871797ac4c6c2f1804c2ab47f2b5fddba2` on `develop`.

| Audit finding | Repair |
| --- | --- |
| DC-01: automatic backend promotion blocked by missing required checks | Restored strict `Release Gate` protection on `develop`, bound to GitHub Actions app 15368. Read back REST and GraphQL settings and verified all other protection settings remained unchanged. Added a read-only preflight before image building and again before promotion. |
| DC-02: newer frontend calls APIs absent from the deployed backend | Added frontend capability discovery against the configured backend's live OpenAPI schema. Claims need list/export operations; release controls need release/promote/roles operations. One shared Query cache refreshes every 30 seconds. Initial loading, failed discovery and unsupported operations keep controls unavailable while draft content remains readable. Rollback discovery removes cached controls. This is operation availability gating, not an atomic frontend/backend deployment. |
| DC-03: workers and scheduler can start before migrations | AWS uses one ordinary digest-pinned migration Job in Argo wave 1 after shared resources in wave 0. API, worker, beat and synthetic consumers use wave 2. API migration init is disabled in AWS and the chart rejects two migration writers. Job identity hashes the exact pod definition; retained completion avoids self-heal reruns. |
| DC-04: duplicate synthetic environment keys | Merge defaults first, then backend settings, then synthetic overrides. Render tests verify unique names and override precedence. |
| DC-05: current operations guidance points to retired infrastructure | Updated infrastructure guidance, chart documentation and engineering gotchas to identify AWS/EKS, RDS, ElastiCache, AWS S3 and the current Argo consumer. Retired overlays remain labeled rollback/render material. |

## Validation

- Full frontend suite: 344 files, 2,556 tests passed before the final additional rollback case; final focused capability/presentation suite: 18 tests passed.
- Frontend type check and changed-file quality/exclusion checks passed. Known full-tree lint debt remains advisory; no quality baseline was weakened.
- Release workflow/gate contracts: 119 passed, including invalid/missing protection cases. Executed the actual read-only preflight against the restored GitHub rule successfully with the local authenticated GitHub account.
- Script contracts: 161 passed.
- Helm 3.13.0 lint/render: base plus dev, staging, production and AWS overlays passed. Existing image, NetworkPolicy and PDB assertions passed; three AWS rollout/environment cases passed, including the image/config/pull-policy Job identity case.
- Compose development configuration passed using an isolated, checksum-verified Compose installation. No containers were started.
- Full workflow actionlint passed with shellcheck disabled (not installed). Python Ruff, Black, isort and added-file mypy checks passed. Directory docs lint passed for 54 documents, and changed Markdown links resolve.
- Alembic static check: one head, 100 revisions, revision IDs within the database limit. No schema migration was executed.

The local CI wrapper's OpenAPI regeneration check could not run because the
local backend environment lacks `langgraph`. Backend HTTP schema, committed
OpenAPI and generated frontend types are unchanged. The wrapper therefore
cannot be reported as fully passing; CI must verify that check with repository
dependencies installed.

## Rollout boundary

The GitHub protection restoration is already applied. All application changes
require merge and the normal release pipeline. No Argo sync, cluster apply,
workload restart, database migration, frontend production deployment or stale
release-PR merge was performed. AWS cluster identity and live Job completion
remain unverified because this environment has no AWS cluster credentials.

Use the full application Argo rollout described by the
[chart migration contract](../../infrastructure/helm/knowledge-graph-analytics/README.md#database-migrations).
Resource-selective sync and standalone Helm upgrade bypass this barrier.
Migrations must remain compatible with old pods still serving during rollout.
A failed migration Job requires diagnosis before an explicitly authorized retry.
