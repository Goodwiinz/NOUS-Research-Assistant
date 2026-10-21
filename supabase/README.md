# supabase/

Supabase CLI project for the NOUS database layer. It holds `config.toml` (local
CLI settings, project id `RAG_system`), 14 timestamped SQL migrations in
`migrations/` (initial schema, user sync to `auth`, row-level-security rollout
and later hardening), and `templates/confirmation.html`, the sign-up email
template.

- The migrations are an ordered history that may already be applied. Append a
  new file instead of editing an old one.
- This history is separate from the backend's Alembic migrations in
  `backend/alembic/` and from the loose SQL in [`database/`](../database/README.md).
  Check the backend and deployment docs ([`docs/engineering/backend.md`](../docs/engineering/backend.md),
  [`docs/engineering/gotchas.md`](../docs/engineering/gotchas.md)) before
  assuming which one a given environment uses.
- `.branches/_current_branch` is a Supabase CLI local-state marker that is
  tracked in the repository; it is a candidate for removal and ignoring, not
  something to edit.

Rules for editing here are in [`AGENTS.md`](AGENTS.md).
