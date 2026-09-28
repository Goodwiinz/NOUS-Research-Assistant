# Academic writing baseline v1

This harness implements the evidence boundary for GOO-293. It preserves the
August 8 baseline and declares a new five-trial canonical comparison plus a
separate development and held-out near-boundary corpus. It does not treat a
chat response, pending task, unit test, provider failure, or unavailable judge
as a writing-quality pass.

## Before a run

Do not edit a corpus or `protocol.json` after collecting trials. A corpus,
protocol, harness, source revision, provider, model, configuration, or seed
change starts a new result. Keep the authenticated runner and judge credentials
outside the retained bundle; trial evidence contains identifiers, hashes,
redacted configuration, and downloaded artifacts only.

Each planned trial must retain a directory containing `trial.json`, the exact
saved draft content, its persisted review, persisted citations, a downloaded
Markdown file, a downloaded LaTeX ZIP, and an independent adjudication bound to
the frozen task and draft hashes. A semantic pass additionally retains a
passing judge calibration that proves a known-correct fixture was accepted and
a known-wrong fixture was rejected, bound to the same reviewer, verifier and
frozen fixture digest. An objective pass records a blocked invalid-revision
review and binds both the before and after snapshots to the retained current
draft id, version and content hash.

The runtime record identifies a unique run, authenticated real-provider
journey, provider, model, digest-pinned runner image, verifier, harness and fixture digests,
pinned tool versions, executed environment flags and redacted model
configuration. Authorization and objective evidence remain required when those
dimensions pass even if a judge or infrastructure component is unavailable.
Durable run, draft, review, adjudication, calibration and authorization
identities and draft/citation/download checksums cannot be reused across planned
trials. The retained trial schema is closed: undeclared runtime fields or
artifact descriptor fields are rejected rather than copied into the report.

The trial file uses four independent verdicts:

- `objective`: `passed`, `failed`, or `not_run`
- `semantic`: `passed`, `failed`, `not_run`, or `judge_unavailable`
- `authorization`: `passed`, `failed`, or `not_run`
- `infrastructure`: `passed` or `failed`

Infrastructure failures and `judge_unavailable` are unscored. They remain in
the published sample-size table but do not become product passes or failures.

## Collect retained evidence

Run from the repository root after all 14 declared trial bundles exist:

```bash
python3 evals/academic-writing-baseline-v1/collect.py \
  --trials-dir /absolute/path/to/retained-trials \
  --output /absolute/path/to/result.json \
  --source-sha "$(git rev-parse HEAD)"
```

The collector verifies that `--source-sha` is the clean checked-out commit, then
recomputes the protocol, corpus, harness, task and artifact digests. It opens the
downloaded LaTeX ZIP, reconciles every saved `[Doc N]` marker with the exact
saved Markdown and transformed LaTeX manuscript bodies, `\cite{docN}` and
parsed BibTeX metadata, and rejects duplicate persisted citation indices. It
binds the persisted review and independent adjudication to the run, task and
draft content hash, verifies judge calibration, and requires authorization
probes to name the exact collaborator, outsider, owning project, foreign
project, resource type and draft. A failed-revision proof is rejected if either
snapshot differs from the current valid artifact. Secret-shaped runtime
configuration, environment, authorization-header and cookie fields are rejected
before a result can be written. The report publishes canonical and
near-boundary verdicts separately, including the declared four-of-five
canonical semantic threshold.

Generated trials and results are ignored by Git. Preserve accepted evidence in
the authorized evaluation artifact store and commit only a dated, redacted
baseline summary after independent adjudication and exact-SHA CI complete.
