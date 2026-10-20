# Proposed organization-scoped handoff slots

Status: proposed for owner design review; no live migration or deployment.
Scope: GOO-423 / HO-4, within GOO-419.

Handoff reads, replay lookup and conflict lookup already use organization,
thread and project. The database currently reserves version and retry-id slots
without organization. When a legacy workspace's owner changes organization,
old records become invisible while still blocking a fresh first save. Both
new-id and repeated-id cases reproduce the failure.

| Slot | Existing unique key | Proposed unique key |
| --- | --- | --- |
| Version | thread, project, version | organization, thread, project, version |
| Retry id | thread, project, handoff_id | organization, thread, project, handoff_id |

Choose the organization-scoped key option from the issue: preserve existing
query boundaries, retain old rows without rewriting, and let the new
organization begin at version 1. The service's race classifier already matches
the preserved `uq_integration_handoffs_` name prefix. No org-agnostic read is
introduced. Normal same-organization uniqueness and replay behavior remain.

Migration `ho04_handoff_org_slots` follows `jd01_document_embeddings` and
replaces both keys transactionally. Downgrade first checks for collisions under
either old key and refuses before changing keys when organizations share a
version or retry id. It never deletes tenant history to make rollback fit.
An operator must resolve such collisions before a later downgrade.

Observed local checks: 44 owning unit tests; native PostgreSQL 15.19 upgrade
with existing history, fresh new-org save/replay, same-org duplicate refusals,
both downgrade collision refusals with data/revision preserved, and clean
downgrade/upgrade replay. The full CI-native lane passed 78 cases without
skips. All blocking local gates passed; targeted migration delta and the
empty-chain replay executed successfully on the owned local server. Generated
API types skipped because HTTP shapes are unchanged. Runtime dependencies
differ from CI requirements; static checks use its pinned minimal environment.

Version-org, retry-id-org and replay-body guards were each removed in turn.
Each focused organization-change test failed with the expected handoff conflict;
exact source bytes were restored and both tests passed. The service itself is
unchanged in the delivered diff. The new native test is included in the
existing zero-skip PostgreSQL CI lane and does not depend on pgvector.

Independent Fable correctness and security reviews completed with no reachable
source defects (sessions `7cd1e339-6000-4d76-8948-96b22069af86` and
`fb66e7e1-220b-45e6-955c-8d78b8688eea`, model Fable, read-only tripwires
false). Their small test improvements are recorded below. Owner acceptance of
this proposal remains separate. Live rollout and downgrade over live data
have not run.

## Final review follow-up, 2026-10-16

The native migration subprocess now matches the sibling full-chain test's
300-second timeout. Duplicate inserts assert the handoff unique-constraint
name, so unrelated integrity errors cannot satisfy the check. The native test
passed again after these changes. No production model or migration changed
after the independent reviews.

Mutation guard locations: both `UniqueConstraint` declarations at
`backend/src/models/integration_handoff.py:15` and `:24`, and `_same_body` replay
matching at `backend/src/services/integrations/handoffs.py:139` (call sites
`:239` and `:271`). Removing organization from
the version key, removing it from the retry-id key, and making replay matching
reject identical bodies each caused the organization-change test to fail
with `HandoffConflict`. The exact restored owning command was:

```sh
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q \
  backend/tests/unit/services/integrations/test_handoff_organization_change.py
```

Restored model SHA-256:
`8df9d1e56f3fc07b88cf36177a5c23e09ba5a2c81df96f89dccfd01a8f70a941`.
Unchanged service SHA-256:
`2c5a6325b6c8093ba4f0df3c14b509bd8d82087419b9108f32218b68085537a0`.
These are synthetic-context service/DDL checks, not a live HTTP grant test.

Before merge, fetch the latest integration branch and check for a new Alembic
head. A live database with differently named pre-existing keys needs operator
inspection; the guarded Alembic drop does not establish that live state.
