# Linear remainder implementation evidence — 2026-10-09

Status: implementation and review evidence from isolated branches based on
`331ef5e84e9bba33234990570eacb9ddb3056150`. This dated record does not
replace the earlier audit, prove deployment, or mark these changes merged.

## Scope and planning sources

The reconciliation covered 266 recent merges, 258 relevant Linear issues,
and 23 planning/guide documents. The follow-up implements reproducible
source gaps; live services and independent expert acceptance remain separate.

Sources: the [Linear harness guide](https://linear.app/goodwiinz/document/nous-local-harness-mcp-and-artifact-workspace-74fa1a817f31),
the [harness amendment](../plans/2026-10-04-harness-plan-amendment.md), and
the [execution plan](../superpowers/plans/2026-10-09-linear-remainder.md).
Full recovered Linear plans were read before implementation:

| Plan | Linear document ID | Implementation boundary |
| --- | --- | --- |
| 02, selected capabilities | `7f817576-0c8d-48d2-b54d-62c95681bd5b` | Separate first migration slice, pending review/merge |
| 03, artifact workspace | `afda841a-2bc5-4bf0-b947-eeef32ace257` | Browser edit backend here; frontend in a separate slice |
| 04, rich context | `a628ca60-451f-44c5-b877-669bfb585fd6` | Requires current-owner design and preceding migration merge |
| 05, bounded workflows | `975eb045-0198-49b5-ae45-57133c586edd` | Requires current-owner design, evaluation and preceding migration merge |

## Implemented repairs and issue mapping

| Linear/audit item | Result in this branch |
| --- | --- |
| GOO-412 | Unsupported random data-key rotation fails safely before key mutation; dry run remains available |
| GOO-413 | Screening queues exclude a user's consumed historical votes while retaining current-tip inputs |
| GOO-414 | Optional graph cleanup failure cannot turn a committed document deletion into an error |
| GOO-416 | Reconnect persists the new credential before retiring a superseded handle |
| GOO-417 | Live-proof cleanup attempts independent resources without masking the primary error; frontend coverage floors raised to the measured passing totals |
| GOO-418 | Access helpers reject missing identities; search filters bind native PostgreSQL enum values; export tests exercise scoped content |
| GOO-87 | Active sidebar uses bounded rendering with keyboard navigation and measured active-row reveal |
| GOO-397/432 | Lease watchdog and socket liveness tests use controlled clocks; queued approval waits for visible state; reconnect mock close dispatches asynchronously |
| AF01 | Stop before first lease cancels atomically; leased recovery rows cannot starve later undelivered cancellations |
| AF02/03 | Codex commands freeze checked, bounded conversation context and explicit edit/regenerate provenance |
| AF04 | Native execute rejects unsupported Codex requests before side effects |
| AF05 | Run-row locking orders terminal checks and appends, preventing nonterminal events after completion |
| AF06 | Action confirmations reach their intended handler and respect the whole-history budget |
| Publication renewal | Durable replay identity follows the same consent, actor, organization, project and thread across renewed grants |
| Browser artifact editing | Immutable text versions, live editor authorization, UTF-8 byte bound, parent conflict detection and durable retry identity |

GOO-415 and AF07 were already implemented in the audited develop revision;
they are not new repairs in this branch. Detailed backend and isolation
evidence is in [backend repairs](backend-backlog-repairs-2026-10-09.md) and
[isolation/sidebar repairs](linear-isolation-sidebar-repairs-2026-10-09.md).

## Artifact and ledger verification

Actual PostgreSQL tests use independent connections and unique disposable
schemas. They cover terminal-versus-announcement ordering, concurrent
reservations across renewed grants, and competing edits against one parent.
Both organization-column and legacy project ownership journeys pass.

The PostgreSQL journey executes the exact committed test source outside its
unrelated integration-wide Qdrant fixture; authentication dependencies and
private storage are fixture adapters. This proves persistence/authorization
ordering locally, not live identity-provider or object-storage availability.

Causal checks observed these regressions fail with their guards removed:

- Run-row lock removal permits a nonterminal artifact event after completion.
- Removing the post-lock reservation recheck allocates duplicate receipts.
- Replacing consent-based lookup with exact-grant lookup breaks renewal replay.
- Removing the artifact parent compare-and-swap lets both competing edits win.
- A delayed identical upload formerly deleted the successful retry's bytes;
  the regression now verifies one version and readable immutable content.

Browser edit tests also cover empty text, invalid Unicode, multibyte size
limits, PNG/PDF/octet-stream rejection, deleted project/workspace and removed
editor membership. API tests verify browser-only access, forged-header denial,
default-off flags, safe current-version conflict details, and authenticated
no-store capabilities backed by the existing server flags.

The combined artifact/store/API/event run passes 105 tests
(`/tmp/rag-artifact-reviewfix-combined.log`). The final PostgreSQL journey passes
both ownership variants (`/tmp/rag-edit-reviewfix-pg-final.log`). Removing the
settled-upload retry guard causes the identical-edit journey to fail with a
publication conflict (`/tmp/rag-edit-replay-pg-mutation.log`); it passes again
after restoration.

Independent review found that the real application's HTTP exception handler
rewrites a router-only conflict response. The new edit route now returns its
typed canonical error envelope directly, preserving its message, status,
current-version detail and no-store policy. The router and actual application
handler variants both fail before the correction and pass afterward
(`/tmp/rag-edit-envelope-red.log`, `/tmp/final-remainder-review-conflict.log`).
Both generated API artifacts were regenerated.

## Coverage and timing follow-up

The frozen sidebar source at `9264fcfd7` passed the full frontend suite and
coverage run: 378 files, 2,917 tests. The matching frontend source in this repair
branch now commits the measured coverage floors: lines/statements 51.66%,
functions 64.75%, branches 80.04%. This intentionally strengthens all four
previous floors. Measurement: `pnpm --dir frontend test:coverage` and
`node scripts/ci/check_frontend_coverage.mjs frontend/coverage/coverage-summary.json`.
The saved measured summary also passes the new comparator values. This amends
the earlier isolation evidence, which recorded the old floors passing.

All three socket liveness cases advance a controlled clock one poll at a time,
allow queued replies to arrive, and assert exact deadline/close behavior. The
healthy case runs beyond its original silence deadline and closes only when
the peer closes. Removing inbound-frame deadline renewal causes a real
liveness-timeout failure (`/tmp/rag-liveness-reset-mutation.log`); restoration
passes the 27 liveness/recovery cases (`/tmp/rag-timing-bridge-restored.log`).
The reconnect mock queues its close event, matching asynchronous WebSocket
delivery and avoiding recursion. The queued approval regression sends its
decision as soon as the modal prompt is visible and asserts the typed denial,
without the obsolete fixed focus delay.

The full terminal run also reproduced fixed-wait failures in slash-completion,
clipboard and delete-confirmation fixtures. They now observe the committed
keyboard focus through a parent effect after child input subscriptions settle,
then send the next key. The slash case failed in isolation before this change
(`/tmp/rag-terminal-slash-isolated.log`). The complete terminal suite now passes
50 tests (`/tmp/rag-terminal-readiness-all-final.log`), and terminal type-check
passes. The complete bridge suite passes 233 tests
(`/tmp/rag-bridge-timing-all-final.log`). No terminal production code changed.

## Decisions

- Publication replay uses historical grants only to identify a receipt. Every
  incoming request still needs live authority; expired grants do not authorize
  new calls. This avoids a new receipt schema while preserving consent scope.
- Browser edits reuse reserve/store/publish and the existing parent
  compare-and-swap. The real PostgreSQL competing-edit proof checks this
  equivalent conflict boundary instead of adding another persistence path.
- Runtime capabilities expose the existing edit/preview flags. The browser
  defaults false on failure; no second flag owner or client environment switch
  is introduced.
- Migration slices stay serial. Preparing the first selected-skills migration
  does not authorize starting sharing, rich-context or workflow migrations.

## Remaining acceptance

No live or expert gate has been reclassified as passed by offline tests.
Pending: GOO-293 authenticated writing trials and judging; GOO-308 two real
consenting projects and restart reconstruction; GOO-309 risk-of-bias expert
review; GOO-311 methods/metafor review; GOO-313 live sandbox; GOO-318 live
Zenodo deposition/resume/hash proof; GOO-319/320 authenticated/expert acceptance;
and GOO-337 configured-model release and rollback proof.

Plan03 sharing/native completion and Plans04/05 remain pending their serial
migration and acceptance prerequisites. The manual same-project handoff from
Plan06 was already implemented; automatic file hooks were explicitly deferred.

## Environment limits

Published follow-up PRs: [repairs #1965](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1965)
and [selected frozen skills #1964](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1964).
Their first migration-check jobs failed while initializing the PostgreSQL
service, before checkout or any migration code ran: Docker Hub returned its
unauthenticated pull-limit error. The test-service images now use the same
PostgreSQL 15 and Redis 7 tags from the [Docker Official Images registry on ECR Public](https://gallery.ecr.aws/docker/).
The workflow still runs every existing migration and integration gate.
This setup correction needs fresh hosted CI; the first failed runs are not
counted as migration failures or as passes.

The backend behavior-test interpreter is Python 3.13; repository CI targets 3.11.
The unchanged architecture AST-hash guard has a known interpreter-dependent
failure under 3.13. A separate Python 3.11 environment with the exact CI lint
pins passes strict typing for all seven added Python files; explicit fixture
annotations remove three errors exposed by that environment. This avoids the
incompatible NumPy 3.13 stubs in the behavior-test environment. The AST baseline
was not rewritten, and no quality floor was lowered. Optional PDF renderers and
live provider checks are identified as unavailable in the linked test evidence.
