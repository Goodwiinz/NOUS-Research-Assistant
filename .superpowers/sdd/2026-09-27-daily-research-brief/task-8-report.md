# Task 8 report — Daily Research Brief certification

Date: 2026-09-28

Branch: `codex/daily-research-brief-20260927`

Task base: `68cc641938f43b99fa8d00e18822ccf2c62106e8`

Original certification commit: `efd76079be75fdbba9eef4215c1c831d829cacb4`

Reviewed source commit: `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`

Disposition: **local follow-up complete; negative ship decision remains**

The executable follow-up is isolated in the reviewed source commit. Its child
commit contains only this report, the verification record, progress, and
bounded transcripts under
`docs/testing/evidence/daily-research-brief-task8-followup-20260928/`. This
two-commit scheme gives the committed evidence an immutable source SHA without
making a commit try to contain its own hash.

## Independent-review findings

All five Important findings have local fixes and direct regression evidence.

1. The UI keeps one-use manual-resume authorization when the immediate refresh
   still returns `paused`/`user_paused`. It clears the authorization at the
   existing terminal and review transitions. The frontend regression performs
   resume POST, paused GET, a real SSE GET, stream claim, and completion; it
   does not stop after asserting the POST.
2. The eval adds a deterministic scorer independent of the pipeline/model's
   self-reported support label. Against the fixed eval corpus it distinguishes
   supported, contradictory, and unsupported claims. A false increase claim
   that quotes real decrease evidence and self-labels supported fails the
   semantic gate. This proves only the fixed-corpus semantic check. The live
   configured-model gate remains `BLOCKED` because `ANTHROPIC_API_KEY` is
   absent, and no live score or citation threshold is claimed.
3. Backend performance again uses the baseline-equivalent 11 max-stage
   observations. The nearest-rank function and all frozen numeric thresholds
   stayed unchanged; p95 is rank 11, the maximum. One warmup remains excluded,
   garbage collection remains enabled, and a deterministic test locks the
   sample policy.
4. Exact documented pytest commands produced the committed bounded
   transcripts. Each transcript names source SHA, command, exit status, and the
   SHA-256 of its local raw capture. The documentation-only evidence commit is
   intentionally separate from the source-under-test commit.
5. The real PostgreSQL integration now races two actual SSE stream claims after
   duplicate resume attempts. It proves one `200`, one `409`, one stage
   execution, one review row, and one durable transition. Removing only the
   row-lock guard produced the required RED `(200, 200)` and duplicate extract
   execution; restoring the exact file produced GREEN.

## Final focused results

| Gate | Result |
| --- | --- |
| Eval file | `13 passed, 1 skipped` in 3.84s; skip is the explicitly `BLOCKED` live call. |
| PostgreSQL lifecycle file | `5 passed` in 4.64s. |
| Stream-claim mutation RED | Expected failure: `(200, 200)` instead of `(200, 409)` and duplicate extract execution. |
| Restored stream-claim GREEN | `1 passed` in 2.70s; production file SHA-256 restored exactly. |
| Focused frontend | 5 files, `61 passed` in 5.21s. |
| Node 24 type-check | PASS with Node 24.21.0 and pnpm 10.18.2. |
| Scoped Python and frontend lint/format | PASS. |

The full PostgreSQL suite continues to cover the six-stage lifecycle, all
three reloadable review gates, partial/all provider failure, evidence levels,
no evidence, verification override, reconnect, stale/duplicate/conflicting
reviews, ownership denials, and exact persisted outputs. The final browser
boundary evidence remains `10 passed (2.7m)` with 200-row review p50 `565.1
ms` and p95 `647.4 ms` below the frozen `999.24 ms` ceiling. One login-redirect
attempt is retained as invalid environment evidence, and host-channel losses
remain excluded rather than relabeled as passes.

## Frozen performance evidence

The exact documented full-file pytest command ran three separate times against
source `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`. All six tests passed in every
invocation. The 11-observation candidate method is directly comparable with
the 11-observation baseline; nearest-rank p95 is the maximum for both.

| Metric | Run 1 | Run 2 | Run 3 | Frozen ceiling |
| --- | ---: | ---: | ---: | ---: |
| Six-stage p95 (ms) | 21.320 | 23.619 | 19.402 | 35.345 |
| Export p95 (ms) | 0.638 | 0.723 | 0.555 | 0.826 |
| 1-provider p95 (ms) | 3.898 | 3.294 | 3.393 | 7.893 |
| 2-provider p95 (ms) | 6.595 | 6.165 | 6.763 | 13.541 |
| 4-provider p95 (ms) | 13.412 | 12.665 | 13.415 | 37.944 |
| Max search p95 (ms) | 14.170 | 13.709 | 13.839 | 38.572 |
| Max screen p95 (ms) | 29.408 | 31.652 | 28.162 | 77.212 |
| Max extract p95 (ms) | 88.468 | 89.163 | 84.095 | 849.648 |
| Peak traced bytes | 3,039,372 | 3,042,789 | 3,034,855 | 4,349,228 |
| Review projection p95 (ms) | 4.767 | 5.641 | 7.071 | 9.282 |
| Cold hydration p95 (ms) | 2.736 | 2.607 | 3.346 | 3.983 |
| Ten-run lifecycle p95 (ms) | 1,179.113 | 1,104.969 | 1,251.171 | 2,068.731 |

All three runs produced the unchanged 15,294-byte export, 200 max-bound rows,
25 screen batches, 25 extract batches, the unchanged 1,152,543-byte max-stage
payload, the 24,094-byte review payload, ten completed lifecycles, zero
duplicate stage rows, and 30 unique review rows.

Earlier failures stay visible. The first candidate failed max search at
`46.135 ms` against `38.572 ms`; an immediate rerun barely passed at `37.816
ms`. Later runs failed search at `48.231 ms`, hydration at `4.016 ms`, and
screen at `191.853`, `190.960`, and `195.187 ms`. Profiling connected those
failures to retained fixture request cycles, a production bound-method cycle,
quadratic scans/copies, repeated schema compilation, discovery scans for
provably unique identifiers, and a private prompt-view deep copy. The repairs
retain exact behavior, public envelope isolation, validation, and frozen
thresholds.

## Release gate ledger

PASS:

- local PostgreSQL, browser, focused backend/frontend, type-check, OpenAPI/type
  drift, disposable migration cycle, frozen performance, Bandit 1.9.4, and
  candidate/staged Gitleaks 8.30.1 checks.

FAILED:

- full `frontend validate`: 113 errors and 1,961 warnings in the existing
  repository-wide lint debt;
- pnpm audit: 21 advisories across 1,900 dependencies, including 2 critical and
  9 high;
- pip-audit: 5 vulnerabilities in 4 resolved packages;
- anonymous legacy Safety: one active `gunicorn 22.0.0` finding, with 129
  automatically ignored unpinned findings; and
- final local CI: 29 repository-baseline backend failures plus broad inherited
  changed-file/debt gates. Its aggregate was `29 failed, 6,293 passed, 81
  skipped, 523 deselected, 3 xpassed`; no remaining failure names a Task 8
  path.

Bandit is a source scan and does not offset either failed dependency audit.

BLOCKED:

- the configured-model eval because `ANTHROPIC_API_KEY` is absent; and
- authenticated Safety because `SAFETY_API_KEY` is absent.

NOT RUN:

- remote branch-rule checks for the candidate SHA, pending repository-write
  authorization;
- production configuration inspection and live rollback drill; and
- deploy, template/feature enablement, or any release action, pending release
  authorization.

The source has no dedicated Daily Research Brief runtime toggle and the local
template loader exposes the template. Production exposure control is unproven.
No push, pull request, deployment, production inspection, or enablement was
performed. The feature stays disabled until every FAILED, BLOCKED, and
release-critical NOT RUN gate is cleared for one exact release candidate.
