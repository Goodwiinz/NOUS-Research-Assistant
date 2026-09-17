# Chat audit release preparation — 2026-09-17

Status: deployment preparation; no source merge, image promotion, production
migration, or live configuration change performed by this task. This is a dated
release record, not a replacement for the [engineering contracts](../engineering/README.md).

The source branch is `codex/chat-audit-20260916`, based on develop
`f5979b6d94a7adf28789895378affcaeb0e17391`. It completes the repairs documented in
the [original adversarial audit](../audits/chat-adversarial-2026-09-16.md) and
[implementation record](../superpowers/plans/2026-09-16-chat-adversarial-repairs.md).
Those records describe their original tested revisions; the additional PR review
and release checks belong to this record.

## Review disposition

The independent review of merge head `81251c4fa52100503683e91dc5bf57798fb4d67c`
identified three important defects and one minor omission:

- A delayed Stop of parked run A could cancel successor B because the database
  UPDATE did not bind the selected run identity.
- After committing Stop(A), checkpoint cleanup could reset B's approval state.
- A database exception while parking was returned as `False`, which streaming
  interpreted as a known competing terminal transition, suppressing approval.
- A propagated GraphInterrupt omitted the accumulated public reasoning summary
  from parked metadata.

Luna max repaired all four findings in
`588a652284417a23e4f11677e76a25ceb4ca1481`. The focused backend suite passed 75
tests, and the five post-format regressions passed. The parent independently
verified the four mutations below against this committed source. The final PR
records broader check results and the successor immutable review disposition.
Changes cross authorization, persistent state and schema boundaries;
they require human review and are not eligible for automatic merge.

## Preparation evidence

The frontend production build passed using Node 24 and the repository's pnpm
build command, dummy local service configuration and no upload credentials.
The production backend dependency stage built on Python 3.11.16 and rendered a
3,285-byte PDF with one parsed page. This verifies the installed native renderer;
the final image build and CI results are recorded on the source PR. Neither local
build proves that the live deployment has changed.

The subsequent full image check exposed existing recipe defects despite passing
PDF, Torch, OpenCV and spaCy smoke checks: the CPU build resolved a CUDA Torch
wheel and then removed two of its dependencies, while the 3.7.1 spaCy model
downgraded spaCy and NumPy against declared requirements. `pip check` reported
four conflicts, so this initial image is not the release candidate.

The corrected production recipe resolves CPU Torch with all requirements in one
transaction using the [official CPU wheel index](https://docs.pytorch.org/get-started/previous-versions/),
asserts that it has no CUDA dependency, and propagates installation failures.
It installs model 3.8.0 without re-resolving application dependencies; that model's
[published compatibility](https://github.com/explosion/spacy-models/blob/master/meta/en_core_web_sm-3.8.0.json)
matches pinned spaCy 3.8.16. The final image must pass `pip check`, model loading,
library imports and native PDF generation during its build. Preserve the normal
non-CPU install path. Require fresh final-image evidence on the PR before release;
the earlier image's successful build alone is insufficient.

Each following test passed before mutation, failed for the named defect with the
mutation applied, and passed after byte-for-byte source restoration. Run from the
repository root with `PYTHONPATH=backend python -m pytest -q <test> --timeout=30`.
The local evidence is under `/tmp/chat-pr-review-20260917/`; it contains no live
account credentials.

| Protection at product head `588a65228` | Focused test selector | Observed mutant failure |
| --- | --- | --- |
| `agent_submission_service.py:444`, exact job predicate in `abandon_awaiting_submission` | `backend/tests/unit/services/agent/test_agent_submission_service.py::test_stale_parked_stop_cannot_cancel_replacement_run` | Removing the predicate returned B's run ID instead of `None` and cancelled B |
| `execute.py:1082`, no checkpoint access after parked cancellation | `backend/tests/unit/api/test_agent_cancel_confirmation.py::test_cancel_awaiting_confirmation_does_not_clear_shared_checkpoint` | Restoring the old cleanup accessed the shared checkpointer after cancellation |
| `finalize_submission`, propagate unknown nonterminal database failure | `backend/tests/unit/api/test_agent_streaming_terminal_guard.py::test_graph_park_failure_keeps_confirmation_terminal_and_never_fails_run` | Restoring exception-to-`False` conversion suppressed the confirmation frame; real finalizer and stream wrapper ran |
| GraphInterrupt parking includes public summary metadata | `backend/tests/unit/api/test_agent_stream_atomic_accept.py::test_propagated_interrupt_parks_public_reasoning_summary` | Removing the field produced `KeyError: 'reasoning_summary'` when checking persisted run metadata |

The first summary mutation harness expected an assertion mismatch; the actual
failure was the missing persisted key. The source was restored in `finally`, the
failure was inspected, and the summary case was rerun with the correct expected
failure and successful restored control. No interrupted mutation is counted.

### Broader CI follow-up

The full GitHub unit selection at `11f2e58fd` completed with 5,645 passed,
9 failed, 80 skipped and 3 xpassed. All nine failures were in the legacy replay
fixture, which supplied neither the canonical workspace/thread access shape nor
the caller-owned run/stream mapping. Commit `6280c8b10` corrects that fixture;
production code is unchanged. All 24 replay-file tests and 26 adjacent resume,
confirmation and rate-limit tests passed locally after the correction.

The parent also verified that the corrected fixtures reach their intended guards.
Using the same pytest command prefix above, these two cases passed, failed with
the respective guard disabled (`200` instead of `204`), and passed after exact
restoration:

- `backend/tests/agent/test_streaming_resume.py::test_resume_mismatched_stream_param_returns_204`
  protects `backend/src/api/agent/execute.py:1334`, the active-stream cursor check.
- `backend/tests/agent/test_streaming_resume.py::test_resume_finished_stream_rejects_wrong_thread_mapping`
  protects `backend/src/api/agent/execute.py:1356`, the immutable stream-to-thread check.

The corrected image at `11f2e58fd` passed `pip check`, a CPU tensor operation, an
actual spaCy NLP pass, OpenCV import, parsed PDF generation and import of all 370
API paths. The network-disabled API probe required the public tokenizer cache to
be supplied first; the initial missing-cache failure is retained in local
evidence. This is not a claim of credential-free live service startup or exact
NLP output parity with the old model. Final-head image and GitHub check results
remain attached to the PR, including the repeat after the fixture correction.

## Observed deployment and rollback identities

Read-only observations on 2026-09-17, before deployment:

| Component | Observed identity |
| --- | --- |
| Kubernetes context / namespace | `do-nyc3-rag-system-cluster` / `rag-dev` |
| Argo CD application | `argocd/nous-dev`, Synced and Healthy, revision `f5979b6d94a7adf28789895378affcaeb0e17391` |
| Backend, Celery worker and beat | All three deployments ready at 1/1 |
| Backend image repository | `registry.digitalocean.com/ragsystemregistry/backend` |
| Backend rollback digest | `sha256:6695df8c9457037dd6d0811afdfd545add38ec65c76ecfb7a46f4b4cf03703e5` |
| Backend rollback tag / sourceSha | `9b6e19b6ede4a3a9def40e09ad373717ebdecab3` |
| GitHub Vercel Production deployment | `6496197810`, success, source `f5979b6d94a7adf28789895378affcaeb0e17391` |
| Frontend rollback candidate | [Successful Vercel deployment](https://nous-9daf4gz3w-md-basit.vercel.app) |

Argo's Git revision is not the backend image's source revision. Recheck these
identities immediately before release. The Vercel connector exposed no accessible
teams, so current domain aliases and automatic promotion settings were not
independently verified. GitHub deployment status establishes a successful
Production deployment, not a verified alias mapping.

## Release order and gates

1. Review the source PR and require every current develop ruleset check:
   Lint Backend, Lint Frontend, Unit Tests, Frontend Tests, Alembic Migration Check,
   Golden Replay, OpenAPI Contract Ratchet, Security Scan, Gitleaks, Integration
   Tests, and Resilience Tests. Strict status checks require an up-to-date branch.
   Resolve any fresh findings or failures before source merge.
2. Coordinate the frontend promotion window before merging source. GitHub records
   show develop changes producing Vercel Production deployments, whereas backend
   image promotion follows a separate PR. Do not assume simultaneous rollout.
   Verify the candidate's backend URL and keep the previous frontend deployment
   available for rollback. Native PDF, durable Stop and summary persistence need
   the new backend; a frontend-only rollout does not verify those repairs.
3. After source merge, the successful develop **Test Pipeline** triggers
   [Release Dev](../../.github/workflows/release-dev.yml). It builds the exact
   tested SHA using the [backend image workflow](../../.github/workflows/docker-build.yml)
   and opens `codex/release-dev-<full-source-SHA>`. Require the image digest and
   `sourceSha` to correspond to that merged source. If develop moves, the workflow
   intentionally defers promotion to the newer tested source.
4. Review the generated image PR, which changes only the backend image identity in
   [values-dev.yaml](../../infrastructure/helm/knowledge-graph-analytics/values-dev.yaml).
   Require its explicitly dispatched Test Pipeline, Secret Scan and Helm Validate
   checks. Older release proposals were still open on this date, including
   [#1660](https://github.com/Goodwiinz/rag/pull/1660); they do not contain these
   repairs. Select only the intended current source and digest.
5. Merge the selected image PR to authorize Argo's rollout. The
   [backend deployment template](../../infrastructure/helm/knowledge-graph-analytics/templates/backend-deployment.yaml)
   runs `alembic upgrade heads` in the new image before starting the API container.
   Confirm it reaches revision `u3v4w5x6y7z8`, whose nullable
   `chat_messages.reasoning_summary` column must exist before new ORM reads.
   Runtime configuration gives `SUPABASE_DB_URL` precedence over `DATABASE_URL`;
   verify the intended database by identity without copying secret values.
   Preserve the current `AGENT_DISPATCH_BACKEND=background` setting for this
   rollout. The API init container owns migrations; worker and beat deployments
   do not run that init step. Enabling Celery chat dispatch later requires a
   separate migration-before-consumer rollout assessment.
6. Observe backend, worker and beat rollout and `/health/readiness`, then complete
   the authenticated smoke checks below against the deployed frontend/backend
   pair. Confirm the actual pod image digest and source SHA; a green source CI run
   alone is not release evidence.

The active workflow is `release-dev.yml`. Comments in legacy deployment workflows
and older deployment guides do not supersede the executable workflow inspected
for this release. Preparation did not dispatch a registry push or merge either PR.

## Acceptance and observation

Use the owner's test account and disposable chats/documents. Record only sanitized
outcomes and run identities, never credentials or customer message bodies.

- Send, stream, reload and resume a chat. Verify one assistant message, preserved
  citations and public reasoning summary, and truthful partial output after Stop.
- Stop while running and while approval is pending. Verify durable terminal state
  after refresh, denied confirmation/resume of the cancelled run, and that a
  delayed old Stop cannot cancel a new run or alter its approval prompt.
- Trigger approval, refresh, approve and complete. Confirm the public summary
  survives interruption and continuation; private model reasoning must not appear.
- Upload valid small documents and reject unsupported/oversized selections with a
  useful error. Retry after a failed upload; ensure composer state remains usable.
- Exercise member versus owner permissions with test data, including missing or
  foreign run/checkpoint identities. A denied action must not produce state changes.
- Export Markdown and PDF. Confirm PDF MIME type, `%PDF-` signature, readable pages,
  server filename and separate known/unknown input and output usage. A renderer
  failure must produce an error rather than an HTML download labeled as PDF.
- Check desktop and narrow mobile layouts: composer, attachments, menus, reasoning
  panel, drawer, long text and keyboard focus without horizontal overflow.

Observe database query rate, connection pool pressure and latency during normal
streaming and Stop. Cancellation polling performs two scalar reads per graph
event; no production load/SLO claim follows from local correctness tests. Also
watch error rate, stalled or orphaned runs, confirmation failures, PDF render
errors, pod restarts and memory. Compare to the pre-release baseline under similar
traffic; no new performance threshold is asserted by this record.

## Rollback

For new authorization, cancellation, persistence, PDF or availability regressions,
stop promotion and preserve sanitized failure/run evidence. If already deployed,
revert the image-identity change through a reviewed GitOps PR to the recorded
previous digest, tag and sourceSha. Argo reconciles Git; `kubectl rollout undo`
alone is not a durable rollback. Restore the previous frontend deployment through
the verified Vercel project if the frontend is implicated.

Keep the additive nullable summary column on application rollback. Do not run the
migration downgrade as routine recovery: it drops stored summary data, whereas
old binaries tolerate the extra column. Drain or reconcile active and parked runs
when crossing application versions, then verify readiness, a new chat and Stop
again. Recheck the live rollback identity before acting if another release has
occurred since this preparation record.
