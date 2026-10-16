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
