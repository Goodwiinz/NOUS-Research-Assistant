# Academic writing baseline v1

This harness implements the evidence boundary for GOO-293. It declares a new
five-trial canonical comparison plus separate development and held-out
near-boundary corpora. Its baseline is not established until all 14 trials run
under this exact protocol. Older `agent-writing-flow` results use a different
protocol and are not comparable evidence. A chat response, pending task, unit
test, provider failure, or unavailable judge is never a writing-quality pass.

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
passing judge calibration that records the input digest, expected outcome and
observed judge outcome for the frozen known-correct and known-wrong fixtures,
bound to the same reviewer, verifier and calibration digest. An objective pass
records a blocked invalid-revision review, recomputes its candidate-content
hash, binds it to the base draft and project, and binds both the before and
after snapshots to the retained current draft id, version and content hash.

The runtime record identifies a unique run, declared seed, authenticated
real-provider journey, provider, model, digest-pinned runner image, verifier,
harness and fixture digests, pinned tool versions, executed environment flags
and redacted model configuration. Every run retains an attestation binding its
run, seed and runner image to the declared commit tree and an empty tracked
diff. All trials in one result must share the same provider, model, runner, verifier, tools,
configuration and environment manifest. Authorization and objective evidence
remain required when those dimensions pass even if a judge or infrastructure
component is unavailable.
Durable run, draft, review, adjudication, calibration and authorization
identities and draft/citation/download checksums cannot be reused across planned
trials. The retained trial schema is closed: undeclared runtime fields or
artifact descriptor fields are rejected rather than copied into the report.
Non-passing trials use a fixed `failure_class`; arbitrary provider or judge
error text is not retained.

The trial file uses four independent verdicts:

- `objective`: `passed`, `failed`, or `not_run`
- `semantic`: `passed`, `failed`, `not_run`, or `judge_unavailable`
- `authorization`: `passed`, `failed`, or `not_run`
- `infrastructure`: `passed` or `failed`

Infrastructure failures and `judge_unavailable` are unscored. They remain in
the published sample-size table but do not become product passes or failures.
The retained boolean `passed` must exactly match the four dimension verdicts,
and every planned trial is explicitly either `canonical` or `near_boundary`.

## Release-gate hook (GOO-307)

`tests/test_release_gate_seeded.py` feeds the pure release gate
(`backend/src/services/research/release_rules.py`) one supported sentence and
one seeded failure per corpus condition: an invented number
(`dev-unsupported-number`) blocks as `model_only`, an opposed claim
(`held-contradictory-sources`) as `opposed`, an assessment citing a link
outside the project (`held-hijacked-id`) as `stale_evidence`, and an
unattributed interpretation (`dev-invalid-revision`) as
`unattributed_interpretation`. It reads only each task's condition, never a
gold judgment, and edits no corpus. It is a hook, not a trial: the 14-trial
baseline above is still required.

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
binds every persisted citation row and the recomputed passing-review candidate
to the draft and project, binds independent adjudication to the run, task and
draft content hash, verifies retained judge results against the frozen
calibration truth fixtures, and requires authorization
probes to name the exact collaborator, outsider, owning project, foreign
project, resource type and draft. The foreign-project denial also requires a
successful project-level probe proving the foreign project exists, so a missing
resource cannot masquerade as an authorization result. A failed-revision proof is
rejected if either snapshot differs from the current valid artifact. Secret-shaped runtime
configuration, environment, authorization-header and cookie fields are rejected
before a result can be written. The report publishes canonical and
near-boundary verdicts separately, including the declared four-of-five
canonical semantic threshold.

Generated trials and results are ignored by Git. Preserve accepted evidence in
the authorized evaluation artifact store and commit only a dated, redacted
baseline summary after independent adjudication and exact-SHA CI complete.
