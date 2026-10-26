# Linear release evidence refresh, 2026-10-16

Status: partial verification, with release and owner gates still open.
Source checkout: fresh `origin/develop` at
`4c4600392c0e4a3705f58b4abcdf6868c8e226bc`. This record adds dated evidence;
it does not rewrite earlier results or close tickets.

Scope: GOO-337, GOO-319 and GOO-320, with the overnight review's remaining
GOO-219/237–241, GOO-419, GOO-435 and GOO-436 commitments carried below.

## Current source and deployment identity

The source default for `DAILY_RESEARCH_BRIEF_ENABLED` is **false**. The live
dev environment opts in. PR [#1955](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1955)
merged on 2026-10-09 as `9ee0933a022016385cdccc07ddf06c77e78fd98b`:
`finalize_search_step` treats the stage envelope as a `Mapping` and retains
attempt history in the receipt journal, preserving the persisted output hash.
The affected lifecycle and route sources are unchanged between the deployed
image source below and this checkout.

Read-only observations from 10:17–10:19 UTC identify the live application as
`nous-dev-aws`, namespace `multimodal-rag-system`, using the
[AWS application manifest](../../../../infrastructure/argocd/applications/aws-dev.yaml).
The canonical [gotchas](../../../engineering/gotchas.md) already distinguish
this from the retired DOKS `rag-dev` lane.

| Boundary | Observed value |
| --- | --- |
| Argo source | `develop`, revision `4c4600392c0e4a3705f58b4abcdf6868c8e226bc`, Synced / Healthy |
| Running image source | `7fbd027ce9f9b7a222c4038edf7ea321416a86dd`, independently resolved from ECR |
| Running digest | `sha256:263a39bfbf68686e9f6a639fef8ccc9ec2d6da02c67cf9a92ffc77c4392a33b1` |
| Consumers | Backend, worker and beat ready, zero observed restarts |
| Migration/extension read | Alembic `jd01_document_embeddings`; pgvector `0.8.2`; embedding table present |
| Enabled in runtime settings | Daily Brief, scheduled search updates and MCP |
| Disabled in runtime settings | Document embeddings, semantic search and page filtering |

The checkout also contains PR #2005's jd01 DDL hardening, which the deployed
image's migration predates. A shared revision name does not prove that newer
DDL implementation ran.

The migration Job's phase, exit status and completion time, along with the
bounded runtime settings/SQL read, are retained in [runtime.json](runtime.json).
No secret payload, user identity, pod dump or environment file is included.
These observations verify the dev lane; they do not settle the release owner's
interpretation of “production” in GOO-337.

## Producer evidence for the running image

Exact-image source Test Pipeline
[38682237647](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/38682237647)
passed at `7fbd027ce9f9b7a222c4038edf7ea321416a86dd`:

| Producer | Job | Result |
| --- | --- | --- |
| Unit Tests | 116249661419 | PASS |
| Integration Tests, shards 1/2/3 | 116253082497 / 116253081345 / 116253081179 | PASS |
| OpenAPI Contract Ratchet | 116249661500 | PASS |
| E2E Tests | 116258912840 | PASS |
| Release Gate | 116264641526 | PASS |

[Release Dev 38687217907](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/38687217907)
passed on that same source: image producer **116264783148**, promotion proposal
producer **116266631541**. Runtime digest and database state were read directly;
a successful build or proposal alone was not treated as deployment proof.
The newer GitOps revision is a separate identity, not a substitute test result
for arbitrary newer product code.

## Current local and authenticated checks

The current-source Daily Brief PostgreSQL suite passed **8 tests, zero skips**
against an owned PostgreSQL 15/pgvector server. It includes lifecycle
durability and disabled-flag rollback behavior. Providers are controlled
fixtures in that suite; this is not configured-model or live rollback proof.

```sh
ORCHESTRATION_TEST_DATABASE_URL=<owned local PostgreSQL URL> \
  PYTHONPATH=backend <prepared backend Python> -m pytest -q \
  backend/tests/integration/test_daily_research_brief_postgres.py
```

The prepared runtime used Python 3.11.13, FastAPI 0.138.1, Pydantic 2.13.4 and
SQLAlchemy 2.0.51. These differ from pinned CI dependencies; the result is not
claimed to be a local reproduction of the complete hosted environment.

At **10:30 UTC**, one normal QA authentication request succeeded, followed
only by authenticated GET requests to `dev-api.goodwiinz.tech`. The live schema
matches the image source's canonical OpenAPI hash. Daily Brief is visible in
the template list and its complete detail loads. All three accessible projects
returned 200 from schedule and review-version list endpoints; each list was
empty. There are no controlled schedule/delta or parent/successor records in
that account to reconstruct or export from this read pass.

[Authenticated probe evidence](authenticated-read-probes.json) retains response
status, byte length and SHA-256, with project identifiers hashed. It contains
no response bodies, account addresses, auth tokens or research content.
Empty lists and mounted routes do not establish the complete R8 journey.

PR [#1862](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1862)
is now merged (`664cee34835cf8f7b34eb50a1685de12daf2f13b`, 2026-10-06),
superseding the older issue text calling it a held draft. The earlier
[real-worker R8 evidence](../../../audits/2026-10-04-live-evidence-r8/README.md)
remains evidence for its declared local processes/source, not this deployment.

## Remaining release and acceptance gates

| Requirement | Current boundary / next evidence |
| --- | --- |
| GOO-337 production target | Release owner must confirm whether dev evidence can satisfy the production wording; no production environment was inferred |
| Owner-only negative control | NOT RUN with a second authenticated account |
| Authenticated Safety | BLOCKED: no Safety API credential was available in the inspected local `/app` preflight; no anonymous scan substituted |
| Configured-model Daily Brief evaluation | NOT RUN; provider connectivity and unit fixtures do not certify the workflow |
| Daily Brief create/start/read/export | NOT RUN as a controlled live journey; list/detail are verified above |
| Live flag-off rollback drill | NOT RUN; only the isolated PostgreSQL behavior passed |
| GOO-319 current worker restart/ticks/delta export | NOT RUN in the deployed lane; retain the earlier worker evidence and current source tests separately |
| GOO-320 parent/successor reconstruction and methods review | NOT RUN in the deployed lane; no empty-list or code-review result is called a methods-expert review |
| GOO-435 / GOO-436 enablement | Semantic/embedding/page flags remain off; no backfill or live feature enablement performed |

## Other overnight commitments

| Issues | Reviewable delivery | Boundary |
| --- | --- | --- |
| GOO-219, GOO-237–241 | [#2009](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2009), endpoint/configuration/auth/connectivity regressions and image-build argument forwarding | Current head `ce6fee6dc`; 3,056 frontend tests passed. Initial current-head integration shard failed on ECR image pull before tests; retry is separate evidence. Earlier head's E2E pass is not claimed for this head |
| GOO-419, child GOO-430 | [#2014](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2014), full selected-context byte accounting, live linked-project bindings, lean access, nullable additive handoff route and adaptive polling | Head `9ba9267f1`; 1,489 API tests passed with explicit test-encryption setup. Hosted unit/contract/frontend gates passed; remaining jobs pending when this record was prepared. Owner acceptance of cross-project browser handoff choice remains separate |
| GOO-419, child GOO-423 | [#2016](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2016), proposed organization-scoped version/retry keys and non-lossy downgrade refusal | Head `5b19d3546`; 44 unit tests, 78 native PostgreSQL lane cases and mutation proof. Owner design acceptance and live rollout remain open |
| GOO-435 | [#2017](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2017), pinned historical primitive-composition decision plus provider diagnostic work | Current corrected historical task: 0/5 objective passes, infrastructure 0, Codex-only PRIMITIVES INSUFFICIENT, NOT GATED. It does not score current composite quality; labelled provider diagnostics are recorded separately |
| GOO-436 | Merged [#1982](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1982), current 40 focused / 350 full read-tool unit passes | Long-page real-provider attempt returned HTTP 404; source tests do not establish provider readiness. Collector failure and any subsequent attempts must remain separate |

Fable correctness/security reviews are source review evidence. They neither
approve owner business decisions nor replace live release or methods gates.
No PR was merged or self-approved; no deployment, live flag, backfill, manual
Linear status or external message was changed by this work.

## Fresh local R8 process/provider check, 11:33 UTC

The unmodified R8 live-proof harness ran at checkout
`0f2541c68ef91eeed0fbad840e8cded4b1f0b2a1` (product source based on the
`4c4600392c0e4a3705f58b4abcdf6868c8e226bc` checkout above). Its eight declared
source hashes still matched after execution. It used a new owned loopback
PostgreSQL database, private harness-owned Redis/worker/beat processes and
real Crossref, PubMed and OpenAlex requests. No deployed application was used.

[Sanitized R8 evidence](r8-local.json) records **28 executed checks passed,
zero failed, one NOT RUN**. The harness prints 29 true checks because its
`crash.not_run` marker is true; that marker is excluded from this pass count.

- The intentional Crossref outage produced unknown results without inventing
  retractions. After that execution was terminal, the first worker shut down
  warmly with exit 0 and a replacement process handled the main update.
  This is a process swap, not recovery of an interrupted execution.
- The main update persisted all five delta classes: 28 new, one changed,
  one unknown, one unchanged and one corrected/retracted. It recorded the real
  Crossref notice and one import receipt.
- Both beat logs contain a tick in the main fire minute. A later read found
  one execution after those ticks. One worker with concurrency 1 serialized
  them; this does not exercise concurrent `SKIP LOCKED` contention.
- The successor carried unchanged decisions by reference, marked uncertain
  carry-over, opened the expected targeted work and reconciled accounting after
  the synthetic review. The original harness re-exports within one session.
  A separate process subsequently used two fresh read-only sessions; both
  reconstructed the parent and successor with their original canonical body
  hashes. Export timestamps are intentionally outside those hashes.
- The archived workspace refused its scheduled update with `project_archived`
  and created no import receipt. All owned child processes stopped, and the
  new database was removed.

The schedule labelled `crash` also fired normally; no worker was hard-killed,
no abandoned attempt was recovered and the production 30-minute stale timeout
was unchanged. Crash recovery is **NOT RUN**. The harness's reviewer/foreign
denials occur in project access before version creation; they do not isolate
the version-creation role gate. Four automatically registered document-processing
health checks returned unhealthy in the local worker logs: the harness's
asyncpg URL reached their synchronous `SessionLocal`. Search-update executions
recorded no failures, but this run does not establish general worker-health
behavior or a deployed health-check defect. One Crossref notice batch returned
429 and recovered on a subsequent request to the same URL.

A first adapter bootstrap failed before any
scenario because synchronous Alembic received an asyncpg URL; its record and
confirmed cleanup are retained separately from this successful run.

Raw evidence, process logs and private adapter sources remain under ignored,
owner-only `evals/jobs/r8-current-local-20261016*/`. The tracked summary keeps
counts, source/artifact hashes and bounded outcomes; hostnames, process IDs,
database URLs, account data and raw provider records are omitted. Fable's
independent source/isolation review found no blocker and identified the
wording boundaries recorded here. This remains a local engineering diagnostic,
not deployed HTTP/MCP, scientific screening or methods-expert acceptance.

## Delivery checkpoint, 11:35 UTC

This checkpoint supplements the earlier table without rewriting its history:

| PR | Current head | Hosted boundary |
| --- | --- | --- |
| #2009 | `ce6fee6dc7bfb2417cd205a3c9a1f289c7ffdb4a` | Test Pipeline 38884795310 attempt 2 SUCCESS; frontend 116930666593, production frontend image and E2E 116936411483, Release Gate 116939976509 passed |
| #2014 | `ee23b5d78032e990bccd267d445928b8f9f308e3` | Native continuation fixture repaired; both PostgreSQL cases pass. Pipeline 38892413820 unit/contract/frontend/migration passed; integration shard 1 producer 116950045104 stopped on ECR quota before tests; other jobs pending |
| #2016 | `5b19d35460491dd2a52c32fd8c1a8bfd9fc0bbf2` | Pipeline 38884010070 SUCCESS, E2E 116929108711, Release Gate 116932263204 passed. The cross-org bot claim was unreachable in a focused reproduction and Fable source review |
| #2017 | `e9d2e4126a97a0a51ae6cea40ffabc0723b74e34` | Pipeline 38892808799 unit/contract/frontend/migration passed; integration shard 1 producer 116951003929 stopped on ECR quota before tests; other jobs pending. The separately declared V3 synthetic provider diagnostic completed; committed source matches all fifteen executed hashes |

The earlier #2021 documentation head `0f2541c68ef91eeed0fbad840e8cded4b1f0b2a1`
also passed unit/contract/frontend/migration checks in run 38892286435, but
integration shard 1 producer 116949944389 stopped on ECR quota before tests.
That is evidence for the earlier head, not a hosted pass for this addendum.

PR #2017 now repairs the same-origin Azure rerank path that caused the earlier
404. Its separate synthetic diagnostic returned every labelled target in the
top three; long-page selection included both labelled relevant pages and the
short document skipped reranking. The historical agent result remains 0/5 and
NOT GATED. These local results leave live flags and broader acceptance open.

## Full local R8 crash-recovery proof, 13:01 UTC

A separate predeclared run at clean source
`556e0bed6783454c6fec0c662152aa11dfedeb24` completed **34 checks passed, zero
failed, zero NOT RUN**. [Sanitized recovery evidence](r8-crash.json) binds
the adapter, eight source files and raw artifacts by hash. Those source hashes,
HEAD and the clean working tree were checked again after execution. The
earlier warm-run record retains its own crash NOT RUN result.

The unchanged harness used real Crossref, PubMed and OpenAlex requests, two
beat processes, private Redis and an owned PostgreSQL database. The database
URL used the synchronous driver expected by Alembic and worker health checks.
The app derives its asyncpg URL normally; the harness mirrored that derivation
for its own async engine. The pre-run declaration's driver wording describes
the harness adaptation rather than every derivation inside the app.
No production service clock, stale constant or signal-ownership guard changed.

- At `12:30:37 UTC`, the harness hard-killed its own active worker process
  group after the first attempt was persisted. An intermediate sample found
  no second attempt; the final three-attempt history shows the retry began
  only after the stale threshold.
- The replacement started the retry at `13:00:57.731996 UTC`,
  **1,820.301978 seconds** after the first attempt, above the unchanged
  **1,800-second** stale timeout. It succeeded at `13:01:01.000077 UTC`.
  The retained attempt sequence is `started, started, succeeded`, from two
  worker identities, for one execution and one import receipt.
- Clock samples before and after the run remained within the predeclared
  three-second database/host skew bound. Actual samples and monotonic duration
  are retained in the sanitized record; the minimum retry assertion was not
  relaxed to that tolerance.
- The main update retained all five delta classes and a real retraction
  notice. The intentional provider outage produced unknown results without
  fabricated retractions. An archived project failed with `project_archived`
  and no import receipt.
- All owned child processes stopped by `13:01:08 UTC`; the scratch database
  was removed. A subsequent read found zero owned proof databases and zero
  native-test schemas; the private Redis port was closed.

Those subsequent read observations are recorded in the orchestrator's tool
outputs. The retained run bundle itself records database removal and child
shutdown. Worker/beat logs use Eastern local time (UTC-4), while this summary
and attempt timestamps use UTC; the schedule also retains its own local-zone
value.

The two beat ticks were serialized by one worker. This does not establish
concurrent `SKIP LOCKED` contention. Review-export repetition in this run is
the harness's same-session check. The separately retained fresh-session
warm-run check and the new native manuscript-package proof have their own
source identities. Worker logs contain no `MissingGreenlet` or `unhealthy`
markers in this run; this observation is not general worker-health acceptance.
Two worker logs recorded a Crossref HTTP 429; the persisted executions
subsequently succeeded. Raw logs, adapter and evidence remain owner-only under
ignored `evals/jobs/r8-crash-local-20261016-v1/`.

This is local process/provider evidence. Deployed HTTP/MCP journeys and
methods-expert acceptance remain open.

## Integrated package proof and CI checkpoint, 13:02 UTC

[PR #2022](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2022), head
`ca0c0cd642a926b18a18f5b74df8b2fb9a7277d4`, connects actual GOO-315 packages
to GOO-319/320 updates. Its native test found that a candidate's own manuscript
journal advanced its frozen PRISMA hash and immediately made promotion stale.
Independent Fable correctness and security reviews found the automatic
statement-bound venue journal did the same. Both reproduced paths are fixed
by excluding exactly those two bookkeeping streams; statement identity,
approvals and venue validation keep their existing content/freshness gates.

The full chain runs with and without statements: a retraction stales the
affected release while an unrelated release stays verified, concurrent linking
retains one link and event, and fresh read-only sessions verify both real ZIP
packages and canonical review exports with the original parent bytes intact.
A separate scheduled update records required backward/forward citation-chase
receipts and reuses them on retry. Seven native scenarios, 1,651 unit/API
tests and four causal guard mutations passed. Both final Fable delta reviews
completed without blockers and with `readOnlyViolation=false`. Hosted Test
Pipeline **38904505690** was still running at this checkpoint.

The following latest-source pipelines completed after the earlier checkpoint:

| PR / head | Successful Test Pipeline | E2E producer | Release Gate producer |
| --- | --- | --- | --- |
| #2014 / `ee23b5d78032e990bccd267d445928b8f9f308e3` | 38892413820 attempt 2 | 116962426110 | 116965574358 |
| #2017 / `e9d2e4126a97a0a51ae6cea40ffabc0723b74e34` | 38892808799 attempt 3 | 116964892664 | 116968246386 |
| #2021 previous documentation head / `556e0bed6783454c6fec0c662152aa11dfedeb24` | 38895622806 | 116968972068 | 116972956581 |

The last row verifies the previous documentation head. This addendum requires
its own hosted checks. #2009 and #2016 retain their exact-head successes above.

Authenticated Safety and the configured Daily Brief model evaluation remain
unavailable: pinned Safety 3.8.1 reports no authenticated session, and normal
checks of the configured Infisical root/app paths and running backend find a
placeholder Anthropic credential, no Safety key and no frozen citation
threshold. No secret values were recorded. Production-environment definition,
owner decisions and live/methods acceptance also remain open. No deployment,
live flags, backfill, grant or workflow creation, merge, self-approval, manual
Linear status change or external message was performed.
## CI registry availability follow-up

After refreshing onto develop, pipeline38958382094 at
`8c868403eee98ed79d4b7c0ddb1684412c83951a` twice failed its shard-1 service pull
before checkout: producers117172667579 and117180791954 returned ECR
`Data limit exceeded`. The missing pytest command and Codecov action were
downstream of the skipped checkout/setup, not application-test failures.

The CI-only follow-up uses the same official Docker Hub PostgreSQL15,
PostgreSQL15-alpine and Redis7 digests proven by successful hosted #2022
producers at `bce9bf7d4d896f401171953caffb52e029cd0ca8`. All four service-image
values and one existing migration expected-image literal change. Parsed
health checks, credentials, ports, triggers, permissions and test commands
remain identical. The retained worker crash experiment stays bound to its
original frozen source and does not become a new experiment result.

[CI proof](ci-registry-proof.json) records image identity and 492 passing CI
contract cases with no failures or skips. Pinned actionlint1.7.7 passes the
whole tree with the exact `SHELLCHECK_OPTS` in workflow-lint.yml; changed-file
Ruff/Black/isort also pass. Current-head hosted checks remain required.

Pins require manual refresh: inspect `docker.io/library/postgres:15`,
`docker.io/library/postgres:15-alpine` and `docker.io/library/redis:7` with
`docker buildx imagetools inspect`, record each matching index digest and
linux/amd64 variant, replace the digest while retaining the tag hint, update
the contract literal and rerun local/hosted checks. Do not refresh a digest
without recording the variant identity and successful required CI.
