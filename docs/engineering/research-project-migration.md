# Research project migration operations

Research APIs use the canonical `Collection.id` as the public project ID. The
internal `ResearchProject.id` remains the engine ID for blueprints, runs, and
steps. A linked engine row stores the canonical ID in
`ResearchProject.collection_id`; responses expose both IDs so operators can
trace either side without rewriting retained child records.

Decision authority is explicit and collection-scoped. The independent roles are
`reviewer`, `adjudicator`, and `supervisor`; one user may hold several
roles. Workspace ownership or administration permits role management, but does
not implicitly grant a decision role. Artifact access also requires an active
workspace membership or ownership and matching effective organization. Public
workspace visibility alone does not grant research-artifact access.

Archived collections and workspaces remain readable. Mutations, including role
changes and engine extension creation, are rejected while archived. The
backfill may link an archived identical-ID project because the operation repairs
identity without deleting history. Soft-deleted projects or ancestors are
reported and never inferred. Foreign-owner, organization, collision, missing
ancestor, and invalid existing-link cases are conflicts.

Linked engine history is retained independently of ORM behavior. PostgreSQL
trigger `trg_retain_linked_research_project` rejects a physical delete of any
`research_projects` row whose `collection_id` is set, using SQLSTATE `23503`.
This preserves blueprint, run, step, and source IDs even if an ORM cascade is
invoked. Soft deletion remains the supported lifecycle operation.

Run the command from `backend/` with the environment's normal database
configuration:

```bash
python -m scripts.maintenance.backfill_research_project_collections
python -m scripts.maintenance.backfill_research_project_collections --apply
python -m scripts.maintenance.backfill_research_project_collections --require-resolved
```

The default command is read-only. It emits deterministic JSON ordered by engine
project UUID with `mapped`, `unresolved`, `conflicting`, and `skipped`
counts and row-level reasons. `--apply` is the only write mode. It takes a
transaction advisory lock plus row locks, writes only safe identical-ID
mappings, and writes nothing when the report contains a conflict. Names are
never used for inference. Re-running apply is safe; valid existing links are
reported as `already_linked`.

Exit codes are:

- `0`: no conflict, and the requested resolution guard passed or was not
  requested.
- `2`: one or more conflicts; apply performed no writes.
- `3`: `--require-resolved` found an active project without a valid canonical
  mapping.

A global `NOT NULL` or equivalent active-project constraint is deliberately
deferred. Deleted legacy engine rows are retained, and populated environments
may still contain active unresolved rows. Before a later enforcement migration,
run dry-run and apply in the target environment, then require a zero exit from
`--require-resolved`. Preserve the emitted report as rollout evidence.

Useful verification commands are:

```bash
PYTHONPATH=backend python -m pytest -q \
  backend/tests/integration/test_research_project_backfill.py \
  backend/tests/integration/test_academic_wave_migrations.py \
  backend/tests/integration/test_research_project_mapping.py

python -m alembic heads
```
