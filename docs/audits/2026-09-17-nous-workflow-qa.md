# NOUS deployment and workflow QA — 2026-09-17

Status at 17:20 UTC: in progress. No live workflow cases have run in this
campaign. The owner requested deep workflow/Q&A testing and a reusable CLI with
HTML/JSON reports, asked testing to wait for deployment, then explicitly
authorized promotion. This dated record supplements the
[release preparation](../deployment/2026-09-17-chat-audit-release.md) and follows
the [QA design](../superpowers/specs/2026-09-17-nous-workflow-qa-design.md).

## Release evidence

- Source PR [#1662](https://github.com/Goodwiinz/rag/pull/1662) was merged by the
  owner. Intended backend source is
  `f733b179c0a23d45bc2f9d71737640203330a4d1`.
- Post-merge [Test Pipeline](https://github.com/Goodwiinz/rag/actions/runs/35248221981)
  and its Release Gate succeeded. E2E evidence is 20 passed and two flaky cases
  that passed on retry: approval denial and delayed thread switching. Both first
  attempts timed out in `page.goto` waiting for `networkidle`; their final pass
  is not evidence of a clean first attempt. The Resilience job selected zero
  tests, so its green status establishes no executed resilience coverage.
- [Release Dev](https://github.com/Goodwiinz/rag/actions/runs/35251645499) is
  building the exact tested source. The executable workflow creates a separate,
  checked GitOps image PR. Neither source merge nor a healthy Argo revision
  alone proves that the new backend image is serving traffic.
- Frontend GitHub deployment `6507645741` succeeded for the intended source at
  `https://nous-anpppplvo-md-basit.vercel.app`. The `goodwiinz.tech` alias still
  needs independent verification.
- At this observation, Argo `argocd/nous-dev` was Synced/Healthy at the source
  revision, but the backend, Celery worker and beat were all ready on the old
  digest `sha256:6695df8c9457037dd6d0811afdfd545add38ec65c76ecfb7a46f4b4cf03703e5`.
  Live workflow testing remains pending new-image rollout and readiness.

## Planned evidence boundaries

The target pair is `https://goodwiinz.tech` and
`https://dev-api.gen-text.app`, Kubernetes namespace `rag-dev`. Use the owner's
account and uniquely marked synthetic resources. Keep exact created-resource
identities for cleanup; never infer ownership from a name search. No stress
test is part of this campaign.

The CLI and exploratory browser campaign will report product failures,
environmental blocks and tool defects separately. Controlled transport tests
must be labeled as such. A single account cannot establish cross-tenant
isolation. Missing live version fields must remain unknown or use clearly
labeled, target-bound operator deployment evidence, never an inferred SHA.

## Workflow results

Pending deployment and independent review of the CLI. Planned cases and
synthetic answer oracles are not executed evidence. Final HTML/JSON artifacts,
reproduction steps, cleanup outcomes and prioritized repairs will be recorded
after execution.

### Pre-promotion baseline amendment

At 17:23 UTC, the existing worker pod was Ready but had 141 accumulated restarts;
the backend and beat had zero. This predates the requested release and must not
be attributed to it. Record new-pod restarts and last termination reasons during
rollout rather than treating readiness alone as a reliability result.

The worker's last termination was `OOMKilled`, exit 137, at 16:59:14 UTC. It has
a 2 GiB memory limit and used 1,195 MiB at the read-only baseline observation.
This is an existing reliability issue, separate from this campaign's product
findings and from any new-pod rollout outcome.

### Promotion candidate at 17:32 UTC

[PR #1663](https://github.com/Goodwiinz/rag/pull/1663), head
`c50c1b7a0ef21724ce205500eaef9dc296422f25`, changes exactly three image-identity
fields in `values-dev.yaml`. The source SHA and tag both identify `f733b179…`;
the immutable image digest is
`sha256:46678e7240e09f697609c3651b4af06c1b8f125d0e8357557e26d15cbdaab508`.
Release Dev succeeded. Promotion Secret Scan and Helm Validate succeeded;
the promotion Test Pipeline is still running. Independent review is in progress.
This is candidate evidence, not a deployment claim.

### Corrected rollback procedure

Independent review reproduced an operational defect in the earlier release
preparation's image-only rollback instructions. The old image's Alembic graph
ends at `t2u3v4w5x6y7`; after the new migration, asking that old graph to upgrade
from `u3v4w5x6y7z8` fails with `Can't locate revision identified by
'u3v4w5x6y7z8'`. The new graph accepts that revision. This was tested offline with
Alembic 1.20.0 and immutable exports of the two source revisions; no live
database was accessed. The complete migration delta is one additive nullable
TEXT column, with no backfill.

If rollback is needed after this migration, use a reviewed GitOps PR that restores
the old image identity **and disables the old image's migration init container**:

```yaml
backend:
  image:
    tag: "9b6e19b6ede4a3a9def40e09ad373717ebdecab3"
    digest: "sha256:6695df8c9457037dd6d0811afdfd545add38ec65c76ecfb7a46f4b4cf03703e5"
    sourceSha: "9b6e19b6ede4a3a9def40e09ad373717ebdecab3"
  initContainers:
    runMigrations: false
```

These are field edits to the existing dev values, not a replacement values file.
Retain the additive column **and** database Alembic revision. Do not stamp the
database backward or drop stored summaries. The new chart's startup script has
no second migration path. Preserve `AGENT_DISPATCH_BACKEND=background`, observe
all three deployments and readiness, and verify a disposable chat/Stop flow.
On a subsequent forward rollout, restore `runMigrations: true` with an image
whose revision graph includes the database's current head. This amendment
supersedes only the earlier image-only rollback procedure; the current promotion
keeps migrations enabled. No rollback has been executed.

## Local diagnostic finding: initial pipeline error is hidden

Status: reproduced at component level on source `f733b179…`; not yet reproduced
against the deployed site. Severity: medium, workflow recovery failure.

In [ResearchPipeline](../../frontend/src/components/research/ResearchPipeline.tsx),
the early `loading || !pipeline` return hides the error banner on the first
failed fetch. The store sets `loading: false` and an error when the request
fails, but `pipeline` remains null, so the component continues displaying an
unlabeled spinner without showing the error or a retry action.

A temporary local probe extended the existing component tests with this state:
`pipeline: null`, `loading: false`, `error: 'Pipeline fetch failed'`. Four
existing controls passed; the new assertion requiring a visible `role=alert`
failed and the rendered DOM contained only the spinner. The production source
was not modified. Initial probe attempts hit local dependency/matcher setup
issues; only the corrected native-matcher run is counted as diagnostic evidence.

Next verification: fail only the owned project's initial pipeline GET in a
controlled browser transport scenario, then verify error visibility and recovery.
Proposed repair: render an explicit first-load error/retry state and prevent a
previous project's pipeline from appearing after a failed project transition.
The latter transition remains a separate unverified hypothesis.

### Local transition diagnostic, 17:36 UTC

The transition hypothesis is now reproduced at store level, still pending
browser verification. A successful load of synthetic project A at step 3 followed
by a rejected load of B leaves A's pipeline in the global store. Calling
`advanceStep(B)` then sends B an update derived from A: step 4 and completed
steps `[0, 1, 2, 3]`. The matching-project control passed; the assertion that no
update should be sent from stale state failed with that exact B request.

This used the real [pipeline store](../../frontend/src/store/pipelineStore.ts)
and mocked service responses, with no remote writes. The token guard correctly
rejects responses superseded by another fetch, but does not bind the existing
pipeline to the mutation's target **before** making the request. Proposed repair:
clear or isolate data when changing project, guard every mutation against
`pipeline.project_id !== projectId`, and make failed loads visibly recoverable.
This establishes a same-account state-integrity defect, not cross-tenant access.

### Frontend identity verification, 17:42 UTC

The public `goodwiinz.tech/login` HTML references assets carrying deployment ID
`dpl_Bfw5F6UnRYbcDuYCxevtuFpp3gJS`. The successful GitHub Vercel commit status for
source `f733b179c0a23d45bc2f9d71737640203330a4d1` points to
[that exact Vercel deployment](https://vercel.com/md-basit/nous/Bfw5F6UnRYbcDuYCxevtuFpp3gJS).
This verifies the observed public frontend against deployment metadata. It is
not a management-API assertion of alias configuration. Direct unauthenticated
access to the generated candidate hostname returned 403; it was not used as
positive identity evidence.

### Promotion merge observation, 17:46 UTC

GitHub reports PR #1663 merged at 17:46:39 UTC, producing
`e81ab704561fc608791166ab3eeaddda25df2e17`. The controller observed this external
merge; it did not issue a merge command or bypass checks. Required branch checks
had progressed, but both promotion E2E jobs and their aggregate Release Gates
were still pending. Do not describe those pending runs as passed. At the first
post-merge observation, Argo still reported source revision `f733b179…`, and all
three running images remained the previous `9b6e19b6…` image. Actual rollout
verification is pending reconciliation of the merged GitOps values.

### Rollout verified, 17:51 UTC

Argo automatically reconciled promotion commit `e81ab704…` and became
Synced/Healthy. Backend, worker and beat rollout commands succeeded. All three
replacement pods were Ready on the exact `46678e7240e0…` digest with source
annotation `f733b179…` and zero restarts at this observation. The previous API
pod was draining and no longer Ready. This short observation does not establish
long-term worker stability.

The API init log confirms upgrade `t2u3v4w5x6y7 -> u3v4w5x6y7z8` and successful
completion. `AGENT_DISPATCH_BACKEND=background` is unchanged. Public `/health`
and `/health/readiness` both returned HTTP 200, respectively `healthy` and
`ready`. The health `version` remains `1.0.0`; image identity, not that generic
version, identifies this release.

The frontend subsequently redeployed at the values-only promotion commit
`e81ab704561fc608791166ab3eeaddda25df2e17`. Its public asset deployment ID
`dpl_DckD41hygX5aCbFuGRhhheCkoYko` matches the GitHub Vercel success status for
that commit. Backend source remains `f733b179…`; those different SHAs reflect
the separate image-promotion commit, not different application repairs.

Owner login succeeded through the deployed UI. Its Supabase session uses
chunked cookies, with no localStorage session; this exposed a QA-adapter gap
and was sent to the tool author for correction. No API fixture calls were made
until the root's separate diagnostic adapter had authenticated successfully.
One private workspace, two projects, one conversation and two threads were then
created and their exact IDs recorded for cleanup.


## Scope amendment — 2026-09-17, chat only

The owner narrowed this campaign to chat. Remaining execution covers chat answers, follow-up context, streaming and Stop, thread history and drafts, chat attachments, public reasoning summaries, exports, authentication, and responsive controls. Research projects, pipelines, notes, and draft generation are excluded from further testing and repair in this campaign. The two exact-owned temporary research projects were removed after the scope change. Earlier observations remain historical evidence.

### Live chat observations after rollout

On backend source `f733b179…` and frontend `e81ab704…`, an exact synthetic answer and a follow-up recalling that answer both passed through the real UI and model, with RAG disabled. Both assistant messages remained rendered after reload and matched persisted API messages. Markdown export retained the marker. PDF export returned a valid signature and parsed as two pages with three marker occurrences. These checks do not yet establish interruption, attachment grounding, or approval behavior. Only campaign-owned chat fixtures were used.

### Chat-only continuation, 18:25 UTC

Both promotion Test Pipeline runs, `35253221818` and `35253224323`, completed successfully after the external merge. This later success does not change the recorded pre-merge timing.

Live UI Stop reached durable `cancelled`, stored a nonempty stopped partial, rendered that partial after reload, and returned idle (204) on resume. Repeated cancellation of the same run returned 204. A diagnostic initially compared Markdown numbering to rendered list text; correcting that oracle resolved the harness failure. Real sidebar switching restored two separate unsent drafts, including Unicode. History search included the matching owned thread and excluded the other. At 390 × 844, Shift+Enter inserted a newline without sending, and there was no horizontal page overflow.

An unsupported `.exe` attachment returned 400, displayed a failed chip, and disabled Send. Removing it restored Send. A supported synthetic TXT upload returned 200, showed its chip, and reached `indexed` with 100% progress. The status oracle was corrected to accept the server's actual `indexed` terminal state.

**CHAT-ATTACH-01 — attachment accepted and persisted, but not available to the answer (high functional impact).** In the owned chat, the user message persisted the exact uploaded document ID. With sources enabled, the question requested only the launch code explicitly present in that document. The answer was “I couldn’t access the attached file.” and contained no citations. This is a live failure of the attachment-to-answer workflow, separate from the successful upload and indexing checks. The synthetic expected code was `KESTREL-DOC-7314`. No unrelated document content is included in this report.

Repair plan: trace validated attachment IDs from submission into retrieval/model context; enforce existing user/organization access checks and bounded content; ground current-turn attachments explicitly, preserve citation provenance, and define honest behavior for unready files. Add failing tests for missing grounding, inaccessible IDs, and no-attachment regressions before implementation. Re-run the known-fact and absent-fact questions after deployment; local unit tests alone cannot establish a deployed repair. A separate Luna max implementation task owns this chat repair.

The QA CLI at initial commit `42ab6d753` is not approved for live use: independent review found gaps in cancellation cleanup, timeout accounting, browser quarantine, write gates, diagnostics redaction, and scenario oracles. The implementation agent is correcting them with negative controls. No live campaign using that initial CLI revision has been run.

### CHAT-RESTORE-02 — pending approval hidden after direct navigation/reload

A synthetic chat requested `forget_memory` with a unique QA-only marker. No approval was sent. The backend stored its user message and reported `awaiting_confirmation`; the resume route returned the confirmation event. Direct `/chat?thread=<owned-id>` navigation and a full reload showed the empty welcome surface: zero transcript messages, no approval controls, and no Stop control. The sidebar nevertheless displayed the persisted message preview and a count of one. A private screenshot records this exact owned-fixture state.

Clicking the same thread in the sidebar immediately restored two transcript nodes and the Approve/Deny dialog without changing the thread URL. Deny sent `confirmed=false` for the exact owned thread; its run reached `completed` and resume returned idle (204). This confirms a restoration defect, while establishing that denial works after sidebar recovery. A separate Luna max task is tracing initial URL/store selection and adding a reproducing regression test before repair. The earlier bounded probe's BLOCKED result is superseded by this diagnosis; no destructive action was approved.

### Further chat controls and transient availability observation

Authenticated empty, assistant-only and 32,001-character agent requests each returned 422 and created no messages in the exact owned thread. Unauthenticated reads of that thread were denied. Reposting the same client-message ID returned the same message identity and one persisted row; Unicode matched exactly. Controlled synthetic assistant Markdown rendered safe bold text while excluding executable script tags, event handlers and JavaScript/data links. This is rendering evidence, not a claim that every injection payload was covered.

With the existing transcript fully loaded through sidebar selection, one controlled browser connection reset surfaced an error and Retry. After asynchronous teardown, Stop was hidden. Retrying produced the exact synthetic answer `RECOVERED-440` and one new user/assistant pair.

**CHAT-READ-03 — transient transcript HTTP 500.** At 18:35:42 UTC, request `da513609ddad8b5e838ca3e9e825de40` to the owned thread message list failed with asyncpg `ConnectionDoesNotExistError`: the database connection closed during the citations read. Backend logs establish this cause. A later read returned 200 at 18:35:47, and the UI displayed the completed answer. This isolated availability failure remains distinct from the successful Retry behavior. Follow-up: examine managed Postgres/pool disconnects and add bounded fresh-transaction retry for idempotent chat reads if disconnects recur; do not retry message writes blindly.
