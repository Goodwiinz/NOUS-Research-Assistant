# GOO-294 backfill dry-run on dev (2026-09-29)

- Environment: AWS EKS `nous-dev-cluster`, namespace `multimodal-rag-system`, host `dev-api.goodwiinz.tech`.
- Deployed backend: `GIT_SHA=1b3ee4d50850d9c02d6df7fba74760142a169051` (GitOps PR #1727, image digest `sha256:58b0fb84…`).
- Migrations applied by the `run-migrations` init container from `u3v4w5x6y7z8` to `merge_daily_harness_20260928` (14 revisions, including `v4w5x6y7z8a9_link_research_projects`, `x6y7z8a9b0c1`, `a3c5e7f901b2`, `b4d6f8021a3c`).
- Command: `python -m scripts.maintenance.backfill_research_project_collections` (dry-run, no writes).

Result (see the JSON beside this file): 1 engine project, 0 mapped, 0 conflicting, 0 skipped, 1 unresolved (`no_identical_id_collection`), `active_invalid: 1`, `guard: not_required`.

The unresolved row is engine project `1bcf8e6f-cffe-4f2b-8839-9165537ab768` ("transferm", created 2026-06-09), with 0 blueprints and no same-named collection. The owner decided on 2026-09-29 to archive it. The script's resolved gate only excludes soft-deleted or linked projects, so the archive must also set `is_deleted=true`; that write was not performed in this session, and `--apply` / `--require-resolved` were therefore not run.
