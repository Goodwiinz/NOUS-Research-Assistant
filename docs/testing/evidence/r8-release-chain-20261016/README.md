# GOO-319/320 persisted update and manuscript package proof

[Evidence index](../README.md) · [Testing contract](../../../engineering/testing.md)

## Source defect and correction

Base: `4c4600392c0e4a3705f58b4abcdf6868c8e226bc` (`origin/develop`,
2026-10-16). Creating a manuscript candidate with a nonempty PRISMA corpus
advanced the `research_manuscript` decision stream included in its own PRISMA
hash. Rebuilding the snapshot immediately afterward changed only
`prisma_body_sha256`; promotion returned `409 Candidate is stale; rebuild`.

Independent Fable correctness and security reviews identified the same failure
for statement-bound candidates: the automatic venue check also advances the
`research_statements` stream after snapshot creation. The parameterized native
test reproduced that mismatch before the follow-up correction.

`manuscript_release_service._prisma` now excludes only the current collection's
manuscript and statements journals. Statement identity and approvals stay bound
directly in the snapshot, and venue obligations are re-read during promotion.
Corpus, screening, acquisition, protocol and other stream inputs remain bound.
A pending candidate is still rejected after the update changes its bound
inputs. Pre-fix candidates with this PRISMA input need rebuilding;
retained package verification and download continue to read frozen bytes.
The project's audit bundle includes bookkeeping stream versions, so its PRISMA
body hash differs from the manuscript package's version metadata.

## Native PostgreSQL scenarios

`test_review_release_chain_postgres.py` connects the existing GOO-319 update,
GOO-320 review and GOO-315 manuscript services in one persisted scenario:

The whole scenario runs both without statements and with a complete statement
set, fixture author attestations and real automatic venue checks.

- Dual screening and full-text acquisition establish the parent review.
- Real draft and manuscript promotions create P citing R5 and Q citing R1.
  Both are verified before the update.
- A controlled provider retracts R5. The accepted update and successor review
  make P stale while Q remains verified. P's immutable database row and stored
  ZIP remain unchanged, and its package and bundle checks pass.
- Assigned update work reconciles, and a new verified package P2 is linked to
  the successor. Two concurrent requests demonstrably contend on PostgreSQL;
  the second replays the first. One link and one journal event are retained.
- Two fresh read-only sessions, each using fresh storage adapters, reload both
  review exports and both real ZIP packages. Canonical export hashes and
  package hashes agree, and the parent bytes equal the pre-update bytes.

`test_search_update_citation_chase_postgres.py` seeds an approved protocol
requiring backward and forward chasing. A real scheduled update records both
receipts with their protocol ID and requirement. A later session reloads those
rows; retrying the same scheduled identities reuses the receipts without
another provider call.

The corpus, approved protocol, run journal, conformant-run status and provider
replies are declared fixtures. The tests do not execute the research engine,
call live OpenAlex, deploy software, enable flags or authorize submission.
The separate real-worker R8 evidence covers worker execution and recovery.

## Reproduction

Use an isolated PostgreSQL database with the normal integration prerequisites.
The fixture creates and drops a unique schema and rebuilds research tables
through their actual migrations. It reads `RESEARCH_DECISION_DATABASE_URL`,
then `ORCHESTRATION_TEST_DATABASE_URL`, then the optional `postgres_container`
fixture when neither URL is configured.

For hosted CI, `screening_factory` also accepts
`ORCHESTRATION_TEST_DATABASE_URL` when its explicit database variable is absent.
The integration workflow already supplies that URL. Previously the fixture
ignored it and fell back to optional testcontainers: a CI-shaped baseline
with testcontainers disabled exited successfully but skipped all three new
cases. The fallback correction ran eleven native scenarios with zero skips,
including the original screening suite, with only the CI database variable
configured and testcontainers disabled. The two new files are assigned to
integration shards 1 (release chain) and 3 (citation chase), respectively.
Hosted execution is verified separately against the published head's producer
and JUnit records; local admission evidence is not a hosted pass.

CI does install testcontainers, so that conditional baseline does not establish
that previous hosted runs skipped these cases. The correction reuses the
already configured job database instead of starting an additional container.
All 22 direct fixture-consumer files were then checked in the same local setup:
39 collected cases passed with zero pytest skips. Three existing fresh-schema
downgrade substeps, guarded directly by `RESEARCH_DECISION_DATABASE_URL`, are
**NOT RUN** in the orchestration-only check (`test_review_versions_postgres`,
`test_search_updates_postgres`, `test_archive_deposit_postgres`). The earlier
explicit-variable native matrix exercised the review/search downgrade paths;
the admission check does not replace that separate evidence.

```sh
cd backend
RESEARCH_DECISION_DATABASE_URL="$TEST_POSTGRES_URL" pytest -q \
  tests/integration/test_review_release_chain_postgres.py \
  tests/integration/test_search_update_citation_chase_postgres.py
```

Local runtime: PostgreSQL 15.19, Python 3.11.13 and SQLAlchemy 2.0.51. The
scratch database does not have the vector extension installed; these fixtures
do not exercise embeddings. This is local source evidence, not a deployed acceptance
result or methods-expert review.

The final owning PostgreSQL matrix passed seven scenarios with zero skips,
including both statement variants and the required chase. The architecture,
API and owning-service unit matrix passed 1,651 tests. Pinned local blocking
checks that ran passed; unchanged-contract and unchanged-migration conditional
checks remain explicitly skipped in the manifest.

## Causal verification

Each mutation was applied alone, restored byte for byte, then rerun green.
The exact command above was focused to the respective test file.

| Guard | Temporary mutation | Observed failure |
| --- | --- | --- |
| Manuscript self-invalidation | Include the own manuscript and statements streams again in `_prisma` | Immediate rebuilt snapshot differs only in `prisma_body_sha256` |
| Review link replay | Disable `link_release`'s stored replay branch | Concurrent second request returns `409 Review version already has a release` |
| Targeted invalidation | Make `review_update_service.graph_part` return no affected nodes or edges | P remains `verified` where `stale` is required |
| Citation receipt replay | Disable `corpus_service.chase_citations`'s pre-provider replay lookup | Four provider calls where two are required |

Raw local logs and JUnit outputs are retained outside tracked source under
`/tmp/nous-linear-review/`: `release-chain-native-v4.log` (reproduction),
`release-chain-native-v7.log`, `release-chain-mutations.json`,
`required-chase-native-v1.log` and `required-chase-mutation.json`. The
sanitized source/result manifest beside this document binds the final proof
files; hosted CI is reported separately for the exact published head.
