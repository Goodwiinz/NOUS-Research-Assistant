# Academic plan-to-write journey v1 (GOO-308)

Evaluates the project Workflow journey (Plan → Discover → Select → Extract →
Write) from persisted evidence only. `protocol.json` is frozen with
`baseline_status: not_established`: this ticket fixes the method, and the
trials establish the baseline.

## Projects

- `real-1`, `real-2` (`consenting_real`): each trial keeps a `consent.json`
  holding the signed consent record id and a scope digest, never the content.
  Until signed consent and real researchers exist, each trial is a
  `trial.json` with a `not_run_reason`. It is reported as `not_run` and never
  simulated.
- `known-answer` (`held_out_known_answer`): ten frozen synthetic documents in
  `corpora/known-answer.json`, which the runner and principals may read. It
  holds one duplicate pair, one report with no retrievable full text, two
  full-text exclusions, three extraction fields and six manuscript claims.
  One claim is a seeded unsupported number and one is contradictory. Only the
  collector reads `corpora/known-answer.gold.json`. Never show it to
  principals, and never tune a product change against it.

## Principals

The principals are the author (the workspace owner, with no decision role),
an explicitly assigned supervisor, reviewers A and B, an adjudicator, a
foreign-org user and a role-less workspace viewer. They must be distinct user
ids. The collector checks each one's roles against the retained `roles.json`
(`GET /roles`).

## Trial layout (`<trials-dir>/<project-id>/`)

| File | Source |
|---|---|
| `trial.json` | `principals`, `scenarios` (`observed` with `evidence` files, or `not_run` with a `reason`), `infrastructure`, `runtime` (with GOO-293's `source_attestation`) |
| `transitions.jsonl` | `frontend/e2e/nous-flows/research-journey.live.spec.ts` plus the runbook steps: `{step, principal, method, path, status, ids, versions, hashes}` |
| `audit-bundle.zip` | `GET /research-engine/projects/{id}/audit-bundle`, downloaded through the rail's button |
| `trace.zip` | the live spec run with `--trace on` |
| `roles.json` | `GET /research-engine/projects/{id}/roles` |
| `independent-review.json` | `{manifest_sha256, reviewer_user_id, claim_correctness}` from someone who is not a principal, confirming offline reconstruction |
| `consent.json` | real projects only |

The runner never writes headers or cookies. The collector runs GOO-293's
`_reject_sensitive_configuration` over every retained JSON line and refuses
the trial on a hit.

## Collector

```sh
python evals/academic-journey-v1/collect.py --trials-dir <dir> --output result.json [--source-sha <sha>]
pytest -q evals/academic-journey-v1/tests
```

The collector reuses GOO-293's evidence helpers through `importlib` and does
**not** import `backend/src`. It re-checks each bundle with stdlib `zipfile`
and `hashlib`: every `SHA256SUMS` line, the manifest, each part's
`body_sha256`, and each released draft's `content_hash` against its stored
`.source.md` content. It also checks the transitions. Every id must exist
before it is referenced, every `next_seq` must never decrease, and a named
hash must never drift. Every created id must appear in the bundle.

If any declared trial is missing required evidence, or a required scenario
has neither evidence nor a `not_run` reason, the collector refuses. It then
exits non-zero and writes no output file.

The measurements follow `protocol.json`. Each is published with its
denominator:

- completion as `k/15`, plus `k/3` per stage;
- participants per principal;
- correction burden, raw and per included report;
- disputed-claim resolution time as n, median and max;
- export completeness as `parts_ok/7`, plus the reconstruction checks.

Adjudicated screening resolutions are not in bundle v1, so the collector
publishes them as `null`.

The verdicts are `reporting_completeness`, `protocol_adherence`,
`claim_correctness` and `infrastructure`. They are never combined into one
score. `infrastructure: failed` and `claim_correctness: judge_unavailable`
are unscored but stay in the sample-size table, as in GOO-293.

## Not run today

These trials are **NOT RUN**:

- the live Playwright trials, which need `RESEARCH_JOURNEY_LIVE=1`, the six
  saved principal states, a deployed stack with GOO-299..308 and #1747, and
  authorization for real-provider spend;
- both consenting real projects;
- the S2 worker restart and the S3 provider outage, which need cluster
  authorization or a safe induction;
- judge-scored `claim_correctness`, which needs judge credentials.
