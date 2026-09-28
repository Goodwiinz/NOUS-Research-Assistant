# Task 8 report — Daily Research Brief certification

Date: 2026-09-28

Branch: `codex/daily-research-brief-20260927`

Task base: `68cc641938f43b99fa8d00e18822ccf2c62106e8`

Original certification source: `efd76079be75fdbba9eef4215c1c831d829cacb4`

Independent-review source: `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`

Final executable source: `ac17ca15a3a91af936851647bbde89d9860709ba`

Prior whole-feature source: `9ca9d6a2995ba8dd52328efd0e5cd93856138b4a`

Disposition: **local follow-up complete; negative ship decision remains**

## 2026-09-28 final re-review amendment

This amendment records the bibliography-provenance repair and corrects the
certification hash invariant. It supersedes the earlier final-source label while
retaining every result under the source on which it ran. The release decision
and every FAILED, BLOCKED, and NOT RUN classification remain unchanged.

The executable changes are frozen in the final executable source. Its child
commit contains only this report, the verification record, progress, and
bounded evidence under
`docs/testing/evidence/daily-research-brief-task8-followup-20260928/`. This
provenance lets every transcript name the exact executable source without a
self-referential commit hash.

## Review findings closed locally

The earlier independent review closed five gaps: the UI preserves one-use
manual-resume authorization through an intermediate paused response; the eval
has a deterministic fixed-corpus semantic scorer independent of the model's
self-label; performance uses the baseline-equivalent 11-observation method;
bounded transcripts record exact commands and source; and the PostgreSQL test
races two real SSE consumers after duplicate resume attempts. Removing the
claim row-lock guard produced the required mutation RED `(200, 200)` and
duplicate extraction; restoring it produced GREEN `(200, 409)`.

The final whole-feature review closed five additional gaps, and the final
re-review closed the remaining multi-provider bibliography and certification
wording gaps:

1. Report rendering joins extraction evidence to canonical sources by
   `source_id`. DOI, year, URL, journal/date, and `evidence_level` come from the
   canonical nested connector shape. Missing canonical bibliography fields are
   deterministically enriched from retained `metadata.provenance[]` origin
   snapshots without overriding non-empty canonical values. Real-shaped
   OpenAlex, Crossref, PubMed, reverse-order, conflict, and bridged-cluster tests
   prove an abstract remains abstract and DOI/year/journal appear in JSON,
   Markdown, and CSV without exposing the provenance blob.
2. Final approval now adds an append-only attestation around the already
   reviewed immutable artifact. It binds the final review ID, reviewer ID,
   timestamp, decision/kind/index, output hash, report hash, and verification
   output hash. `report_hash` is the canonical JSON report hash; the exact
   persisted Markdown prefix has its own SHA-256 in the post-approval audit;
   and the export-stage `output_hash` binds the full persisted envelope,
   including `report_hash`, `verification_output_hash`, and Markdown content.
   Verified JSON, Markdown, and CSV expose the full review history and
   attestation; missing, mismatched, or corrupt attestations fail closed.
3. `DAILY_RESEARCH_BRIEF_ENABLED` is a server-owned setting with default
   `false`. When false, the Daily template is absent from list/detail, cannot
   create a Daily blueprint, and cannot start a new Daily run even from an
   existing blueprint. Existing persisted runs remain readable/exportable for
   audit and recovery; legacy/custom workflows remain available. Tests and E2E
   opt in explicitly.
4. The complete 10-scenario browser suite ran against exact source
   `ac17ca15a3a91af936851647bbde89d9860709ba` with the feature flag enabled in
   the isolated test environment. It includes the manual-resume handoff and
   200-row bound and passed `10/10` in 2.6 minutes.
5. Cancellation recovery locks and rereads durable state before deciding to
   pause. A disconnect after a completed or failed terminal commit can no
   longer rewrite the run to paused. Removing only this terminal guard produced
   mutation RED (`completed` became `paused`); exact restoration produced
   GREEN. Existing manual pause/reconnect coverage stays green.

## Final exact-source results

| Gate                             | Result                                                                                  |
| -------------------------------- | --------------------------------------------------------------------------------------- |
| Affected backend                 | Final source: `161 passed` in 7.45s.                                                     |
| Broader focused backend          | Prior whole-feature source: `277 passed` in 14.85s.                                      |
| PostgreSQL lifecycle             | Prior whole-feature source: `5 passed` in 4.88s; final browser exercised real PG.        |
| Terminal-cancellation mutation   | Expected RED `1 failed`; restored GREEN `1 passed`.                                     |
| Focused frontend                 | 5 files, `61 passed`.                                                                   |
| Exact-source browser             | `10 passed (2.6m)`; 200-row p50 `489.2 ms`, p95 `542.1 ms`, frozen ceiling `999.24 ms`. |
| Frozen performance               | Prior source `9ca9d6a`: `6 passed` in 18.93s; every frozen ceiling passed.              |
| Node 24 type-check               | PASS with Node `24.21.0` and pnpm `10.18.2`.                                            |
| OpenAPI and generated TypeScript | PASS; regeneration produced no diff.                                                    |
| Scoped Ruff/format               | PASS.                                                                                   |
| Bandit 1.9.4                     | PASS; no in-scope source finding.                                                       |
| Staged Gitleaks 8.30.1           | PASS; no staged-source secret finding.                                                  |

The final provenance regression was first RED on the default OpenAlex-first
same-DOI path (`publication_year` was `None` instead of `2025`) and GREEN after
the fallback was added. The final affected suite includes reverse provider
order, canonical conflicts, and a three-provider DOI/PMID bridge. Bounded
RED/GREEN and exact-source transcripts record the raw-capture digests.

The exact-source browser scenario performs resume POST, observes the immediate
paused GET, starts the SSE request, confirms the stream claims the durable run,
and reaches the next controlled lifecycle boundary. Because the local Next
proxy buffers the deliberately held SSE response, the deterministic test
confirms `running` through the real audit route and sends the pause through the
same authenticated browser → Next → FastAPI boundary. It therefore proves more
than issuance of a resume POST.

Earlier browser failures remain visible in committed evidence: one source had
an invalid consumed-response assertion (`1 failed, 9 not run`), another had an
unstable reconnect fixture (`5 passed, 1 failed, 4 not run`), and three focused
attempts exposed local Next response buffering. An incomplete-auth run and host
channel losses are excluded environment evidence, never relabeled as passes.

## Frozen performance evidence

Three baseline-equivalent full invocations passed on independent-review source
`a92fcfa`, and one exact documented full invocation passed on prior
whole-feature source `9ca9d6a`. The final re-review changed only report
metadata projection and its tests, so performance was not rerun or relabeled.
The frozen numeric thresholds and nearest-rank percentile function
never changed. Both baseline and final method use one warmup plus 11 measured
max-stage observations with GC enabled, so p95 is rank 11, the maximum.

| Metric                        |                  Source 9ca9d6a |                 Frozen ceiling |
| ----------------------------- | ----------------------------: | -----------------------------: |
| Six-stage p95                 |                   `19.962 ms` |                    `35.345 ms` |
| Export p95                    |                    `0.595 ms` |                     `0.826 ms` |
| Export payload                |                    `15,498 B` |                     `18,353 B` |
| 1/2/4-provider p95            |   `3.524 / 6.428 / 13.190 ms` |   `7.893 / 13.541 / 37.944 ms` |
| Max search/screen/extract p95 | `15.974 / 31.788 / 96.698 ms` | `38.572 / 77.212 / 849.648 ms` |
| Max-stage payload             |                 `1,152,543 B` |                  `1,383,052 B` |
| Peak traced bytes             |                 `3,034,368 B` |                  `4,349,228 B` |
| Review projection p95         |                    `6.145 ms` |                     `9.282 ms` |
| Cold hydration p95            |                    `3.096 ms` |                     `3.983 ms` |
| Ten-run lifecycle p95         |                `1,149.907 ms` |                 `2,068.731 ms` |

The final export grew by 204 bytes because canonical bibliography and approval
attestation data are now represented correctly; it remains below the unchanged
ceiling. The run produced 200 candidates, 25 screening batches, 25 extraction
batches, 10 completed PostgreSQL lifecycles, zero duplicate stage rows, and 30
unique review rows.

Earlier performance failures remain part of the record: max search `46.135 ms`
and `48.231 ms`, hydration `4.016 ms`, and screen `191.853`, `190.960`, and
`195.187 ms`. The measured repairs removed fixture/executor cycles, a production
bound-method cycle, quadratic scans/copies, repeated schema compilation,
unnecessary unique-identity discovery scans, and a private prompt-view deep
copy. Public envelope isolation, validation, exact behavior, thresholds, and
the final baseline-equivalent measurement method remain intact.

## Release gate ledger

PASS:

- local PostgreSQL, exact-source browser, focused backend/frontend, type-check,
  OpenAPI/type drift, disposable migration cycle, frozen performance, Bandit,
  and staged Gitleaks checks.

FAILED:

- full `frontend validate`: 113 errors and 1,961 warnings in existing full-tree
  lint debt;
- pnpm audit: 21 advisories across 1,900 dependencies, including 2 critical and
  9 high;
- pip-audit: 5 vulnerabilities in 4 resolved packages;
- anonymous legacy Safety: one active `gunicorn 22.0.0` finding, with 129
  automatically ignored unpinned findings; and
- full local CI: 29 repository-baseline backend failures plus inherited broad
  changed-file/debt gates (`29 failed, 6,293 passed, 81 skipped, 523
deselected, 3 xpassed`). No remaining failure names a Task 8 path.

Bandit is a source scan and does not offset either dependency-audit failure.

BLOCKED:

- configured-model eval because `ANTHROPIC_API_KEY` is absent; and
- authenticated Safety because `SAFETY_API_KEY` is absent.

The deterministic fixed-corpus semantic eval passes, but it is not a live-model
pass or a general entailment benchmark.

NOT RUN:

- candidate-SHA remote branch-rule checks, pending repository-write authority;
- production configuration inspection and live rollback drill; and
- deployment or production flag enablement, pending release authority.

## Release and rollback control

`DAILY_RESEARCH_BRIEF_ENABLED` defaults to `false`; no production value was
inspected or changed. A future authorized enable sequence is: deploy the
backend and additive migration while the flag remains false, complete all
release gates for the exact deployed candidate, inspect owner-only production
controls, set the flag true, restart/roll out the backend configuration, and
smoke list/detail/create/start plus existing-run read/export.

Rollback begins by setting `DAILY_RESEARCH_BRIEF_ENABLED=false` and applying
that configuration before reverting application code or schema. Confirm that
list/detail/create/start are hidden or refused while existing persisted runs
remain readable/exportable. Keep the additive review table through the normal
migration rollback window.

No push, pull request, deployment, production inspection, or enablement was
performed. The ship gate remains closed until every FAILED, BLOCKED, and
release-critical NOT RUN item is cleared on one exact release candidate.
