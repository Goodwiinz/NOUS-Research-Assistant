# nous-verify: user-level verification gate — design

Date: 2026-10-08. Status: approved (approach A). Implementation plan: to follow.

## Problem

Agent loops stop when work *looks* done. NOUS has strong unit/CI gates, but
`docs/engineering/nous-loop.md` step 7 explicitly disclaims end-to-end
coverage, and recent work repeatedly closed with "live proof NOT RUN" or
"nothing browser-verified". The app has drivers (three Playwright suites, the
`tests/e2e/qa` NOUS QA CLI, `tools/nous-playwright`, `./nous "<prompt>"`
one-shot) but nothing selects them from a diff, keeps visual proof, or blocks a
loop outcome on them.

## Decision

Build on the existing `tests/e2e/qa` runner (approach A). Rejected: a new
standalone suite (fourth Playwright setup, duplicated auth/reporting) and a
prose-only skill with ad-hoc browser driving (not repeatable, cannot gate).

Targets, by phase: **local** (pre-merge gate) and **goodwiinz.tech /
dev-api.goodwiinz.tech** (post-deploy confirmation).

## Components

1. **Contract + skill.** `docs/engineering/verification.md` is canonical.
   `.claude/skills/nous-verify/SKILL.md` is a thin adapter: select features
   from the diff → boot or target → run scenarios → inspect checkpoint
   screenshots → write evidence → report `PASS` / `FAILED` / `BLOCKED` /
   `NOT RUN`.
2. **Feature map.** `docs/engineering/feature-map.yaml`, one entry per feature:
   `id`, `surfaces` (web route, API endpoints, CLI), `states` (empty, loading,
   streaming, HITL-pending, error, done), `pass_criteria` (observable
   assertions), `scenarios` (QA scenario ids), `owns` (path globs for diff
   selection). v1 journeys: login; chat send → stream → reload persists; HITL
   approve and deny; document upload + attach; project creation via chat.
3. **Flow charts.** `docs/engineering/flows/<feature>.md`, one Mermaid chart per
   journey; nodes carry checkpoint names that scenarios screenshot.
4. **Driver changes** in `tests/e2e/qa`: `--features`,
   `--changed-from <ref>` (map-driven selection), a `checkpoint(name)` helper
   (full-page screenshot), video + trace always on, `--evidence-dir`.
   `scripts/verify/boot_local.sh` starts the backend (`backend/.venv`) and
   frontend (`dev:offline`) and waits for health.
5. **Proof.** Binaries (PNG, webm, trace) go to gitignored
   `.verify-artifacts/<run>/` and are uploaded as CI/PR artifacts. A committed
   text record goes to `docs/testing/evidence/verify-<feature>-<date>/README.md`
   (SHA, target, per-scenario result, checkpoint list). Passing assertions is
   not sufficient: the agent must view the checkpoint screenshots and confirm
   the pass criteria visually.
6. **Stop gate.** New `nous-loop.md` step **7b "Prove it as a user"**: if the
   diff touches a mapped feature, the outcome cannot be `merged` without a
   local `PASS`; `BLOCKED` / `NOT RUN` forces `ready-for-human`. Step 8 adds a
   post-deploy rerun against goodwiinz.tech with `--expected-backend-sha`; a
   failure files a regression instead of closing.
7. **Upkeep.** `scripts/ci/check_feature_map.py` fails when a mapped scenario
   id does not exist, a flow checkpoint has no matching `checkpoint()` call, or
   a new `app/**/page.tsx` or API router is not claimed by a feature and not on
   the shrink-only ignore list.

## Risks

- **Local boot needs reachable data services.** No local Docker; the backend
  must reach dev Postgres/Redis (CGNAT has broken DO trusted-source IPs
  before). Fallback: Vercel preview + dev API, reported honestly as `BLOCKED`
  when neither works.
- **Credentials.** `NOUS_QA_EMAIL` / `NOUS_QA_PASSWORD` must be provisioned;
  without them every run reports `NOT RUN`, never `PASS`.

## Delivery

Three PRs, each built and reviewed by Fable subagents:

1. Contract, feature map, flow charts, `check_feature_map.py`.
2. Runner changes: checkpoints, evidence output, diff selection,
   `boot_local.sh`.
3. `nous-loop.md` step 7b + post-deploy rerun, plus the skill adapter.
