# R8 live evidence: scheduled search updates and review versions (2026-10-04)

Retained records for GOO-319 and GOO-320 from a real-process run: a real Celery worker and beat, a private Redis, a scratch PostgreSQL 14 database migrated with the real Alembic chain, and live Crossref, PubMed and OpenAlex requests. This is dated evidence for the code at `develop` `755312ffc` (the R8 services are unchanged since they merged as `f59725d87` and `ebcf2ce0b`). It is not a deployed-stack result: dev cannot receive the R8 migrations until Release Dev is repaired.

Per-ticket results: [`../2026-10-04-goo-319-live.md`](../2026-10-04-goo-319-live.md) and [`../2026-10-04-goo-320-live.md`](../2026-10-04-goo-320-live.md). The plan and its as-executed amendment: [`../../plans/2026-10-04-academic-r8-acceptance-closure.md`](../../plans/2026-10-04-academic-r8-acceptance-closure.md).

## Files

| File | Holds |
| --- | --- |
| `journey.md` | Timed steps of the evidence run and the check names each step proves |
| `checks.json` | The 34 checks of the evidence run, each with its observed detail (all passed) |
| `shakedown-checks.json` | The 29 checks of an earlier run without the crash scenario (all passed) |
| `timeline.json`, `state.json` | Process events and execution state changes with timestamps; world and schedule identifiers |
| `scenario-outage.json`, `scenario-main.json`, `scenario-crash.json`, `scenario-revoke.json` | One execution each with every attempt, plus coverage, counts and the retracted-DOI item |
| `delta-main.json` | The sealed `nous.academic.search-delta.v1` export of the main fire |
| `snapshots-main.json` | Baseline and post-fire corpus snapshot digests and the delta hash |
| `review-chain.json`, `review-version-root-export.json`, `review-version-successor-export.json` | The GOO-320 chain: both versions, accounting before and after review, and both sealed exports |
| `log-summary.json`, `log-excerpts.txt` | Tick counts per process and the log lines that show tick handling, the restart and shutdowns (full logs stay outside the repository) |
| `ci-jobs.md`, `junit-r8-proofs.xml` | The named producer CI jobs for the PostgreSQL proofs and a local rerun on the same SHA |
| `spike-providers.json` | The live provider results used to pick the baseline fixtures, and the Crossref notice probe |

## How it was produced

From `backend/`, with a cleared environment keeping only `PATH` and `HOME` and the scratch database named in `DATABASE_URL`:

```
DATABASE_URL=postgresql://<local user>@127.0.0.1:5432/<scratch db> python -m alembic upgrade head
DATABASE_URL=postgresql+asyncpg://<local user>@127.0.0.1:5432/<scratch db> REDIS_URL=redis://127.0.0.1:6391/0 \
  PYTHONPATH="$PWD" python scripts/validation/r8_live_proof.py --out <evidence dir> --scratch <temp dir> --allow-hard-kill
```

Alembic needs the synchronous URL; the harness needs the asyncpg one. The harness signals only the processes it spawned, uses a per-run random field-encryption key that is never stored, and leaves `CROSSREF_MAILTO` unset. Seeded users use `@test.invalid` addresses and no address appears in these files. The Lancet DOI in the baseline is a real fixture for a real retraction record, used only for its public bibliographic metadata and Crossref notices.

## Left behind

Nothing was deleted. Local PostgreSQL databases `r8_proofs_20261004` (proof reruns), `r8_live_20261004` (shakedown) and `r8_live_20261004b` (evidence run); worktrees `RAG_system-wt-r8-live` and `RAG_system-wt-r8-flag`; the private Redis data directory and full process logs in the Claude session scratchpad. The Redis instance and all Celery processes were stopped at the end of each run.

## Not covered

The rag-dev journey, Kubernetes pod restarts, missed-fire coalescing, claim and release staleness, and the methods-expert review of the update accounting are listed per ticket in the two write-ups. A passing local run does not replace them.
