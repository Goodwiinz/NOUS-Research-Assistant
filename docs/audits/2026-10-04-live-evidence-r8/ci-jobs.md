# Named producer CI jobs: R8 PostgreSQL proofs

Recorded 2026-10-04 against `develop` at `755312ffc`: Test Pipeline run [37180278410](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/37180278410), conclusion `success`, started 2026-10-04T05:34:21Z. Release Gate in the same run: success.

| Ticket | Proof | CI job | Result |
| --- | --- | --- | --- |
| GOO-319 | `backend/tests/integration/test_search_updates_postgres.py::test_schedule_claims_once_classifies_and_survives_restart` | [Integration Tests (shard 2/3)](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/37180278410/job/111372762790), job `111372762790` | PASSED (log line 3173). Shard summary: 111 passed, 22 skipped |
| GOO-320 | `backend/tests/integration/test_review_versions_postgres.py::test_superseding_review_reconciles_carries_forward_and_targets_staleness` | [Integration Tests (shard 3/3)](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/37180278410/job/111372762721), job `111372762721` | PASSED (log line 2944). Shard summary: 142 passed, 28 skipped |

Neither test was skipped: each named test id appears in its shard log followed by `PASSED`.

## Local rerun on the same SHA

The same two proofs, run from the `docs/r8-acceptance-closure` worktree (base `755312ffc`) against a scratch database on PostgreSQL 14.23 (Homebrew):

```
RESEARCH_DECISION_DATABASE_URL=postgresql://<local user>@127.0.0.1:5432/r8_proofs_20261004 \
  pytest -q tests/integration/test_search_updates_postgres.py tests/integration/test_review_versions_postgres.py \
  -p no:cacheprovider --no-cov --junitxml=junit-r8-proofs.xml
```

Result: **2 passed in 6.46 s**. The retained report is [`junit-r8-proofs.xml`](junit-r8-proofs.xml).

## What these proofs do and do not establish

They exercise the real PostgreSQL constraints, triggers and service layer with fake provider connectors and an in-process simulated crash. They do not exercise a real Celery beat or worker process, live providers or the deployed stack. The real-process and live-provider results are in the other files of this directory.
