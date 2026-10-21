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
configured and testcontainers disabled. At head
`aaed1d70773d32e9099d7199e91a205dcaeefed7`, the two new files were assigned to
integration shards 1 (release chain) and 3 (citation chase), respectively.
Hosted execution is verified separately against the published head's producer
logs; local admission evidence is not a hosted pass.

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

The original owning PostgreSQL matrix passed seven scenarios with zero skips,
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

## Retraction persistence follow-up

The original results and source hashes in `proof.json` are retained. The
separate [persistence proof](persistence-proof.json) binds this follow-up.

Fable's methods-evidence review raised a possible status reversal. A native
probe confirmed it at `aaed1d70773d32e9099d7199e91a205dcaeefed7`: accepting a
later weekly delta whose publication check is unknown changed P from stale
back to verified, despite the retained earlier retraction. P's bytes were
unchanged. The permanent regression failed at that exact status assertion.

Accepting a successor now invokes the existing draft-release invalidator in
the same transaction as the review version and event. The strict ledger
recognizes `research_review_update` as a cause, linked to the exact review
event. Existing dependent draft releases stay invalidated when a later check
is unknown; unrelated releases remain verified. Manuscript rows and stored
package bytes remain immutable.

Both native statement variants pass after two accepted deltas. They verify
one durable invalidation after acceptance replay and join its cause to the
correct review event. The existing review-version proof also passes. There
are three distinct native cases; the restored plain case overlaps the first
matrix. The unit/API/architecture and ledger matrix passes 1,765 cases with
zero skips. Omitting only the durable invalidation call reproduces the status
failure; exact restoration passes again. Applicable local CI gates pass.

The stamp applies when accepting a successor. Historical accepted updates and
any already-revived statuses require separate assessment before rollout.
Protocol approval, conformant-run status and providers remain fixtures; this
does not establish deployed acceptance or human methods-expert approval.

## CI image delivery follow-up

At `fd81c64`, integration shards 1 and 2 passed, but shard 3 failed three
times before checkout: the public ECR mirror returned `Data limit exceeded`
while pulling PostgreSQL. Test and Codecov failures after missing checkout
are downstream; the extended package cases were not executed in that shard.

The initial change at `711feb3` moved the integration PostgreSQL image to
Docker Hub; its [original registry record](ci-registry-proof.json) is retained.
The next run exposed the same ECR quota on the migration PostgreSQL Alpine
image. Fable also identified the remaining Redis pull as quota exposure.

All four service-image references in this Test Pipeline now use the
[official PostgreSQL](https://hub.docker.com/_/postgres) and
[Redis](https://hub.docker.com/_/redis) images from Docker Hub, pinned to the
exact three OCI digests observed in passing jobs. Major/variant tag hints
remain visible. Separate manifest inspections verify each tag-plus-digest
reference and retain argv, exit codes, timestamps and output hashes.

The [complete registry proof](ci-registry-proof-v2.json) records the four
changed YAML image values. Database setup, credentials, permissions, test
commands and required gates remain identical. One existing migration CI
contract assertion tracks the verified PostgreSQL Alpine image. The CI
contract suite passes 492 cases, and pinned actionlint 1.7.7 passes for the
whole tree. Explicit command results accompany silent logs; an empty log's
hash alone is not pass evidence. Actual runner downloads and test execution
still require the new published-head CI.

### Refreshing the image pins

These workflow-service digests require explicit refresh for image/security
updates. Inspect the approved major/variant tags with Docker tooling:

```sh
docker buildx imagetools inspect docker.io/library/postgres:15
docker buildx imagetools inspect docker.io/library/postgres:15-alpine
docker buildx imagetools inspect docker.io/library/redis:7
```

Record the index digest and Linux/amd64 descriptor. Update the four service
references and the existing migration contract assertion together, retaining
the intended major versions and variants. Add a dated identity/check record,
run pinned actionlint and the CI contract suite, and require full hosted CI.


## Accepted publication history follow-up (2026-10-17)

The earlier persistence fix protected draft releases already present when a
review accepted a correction or retraction. A later unknown publication check
still hid that notice from a new draft's promotion graph. The new native
regression reproduced an unsafe promotion at `bce9bf7d`.

`graph_part` now reads retained accepted deltas from the same project and
keeps every accepted correction or retraction, alongside ordinary changes
from the latest delta only. Merged reports resolve to their surviving identity.
Unknown, omitted and later no-notice results cannot erase known publication
evidence. Unaccepted executions add nothing. This also protects new draft
promotions without a prior release row to invalidate.

The separate [publication history proof](publication-history-proof.json) binds
this source and its final tests. Restoring the old graph function makes the
new promotion assertion fail; exact restoration passes both native statement
variants and the existing review-version scenario (three cases, zero skips).
Seven unit histories cover notice persistence, omissions, ordinary changes
and merged identities. The broader initialized unit/API/architecture matrix
passes 1,785 cases. The initial broad run lacked synthetic encryption setup;
its four setup errors remain retained separately. Pinned local gates that ran
pass; unchanged generated-type and migration checks remain skipped.

Fable's independent read-only correctness/security review found no blocker.
The primary matched all three source fingerprints afterward; the reviewer
read source and proof records without running tests or recomputing hashes.
The owned PostgreSQL container was removed after confirming zero test schemas.

Retention is conservative: no correction-clearance or waiver policy is added.
A later accepted event that first reaches a live release supplies that
invalidation's cause; the historical notice remains in the review chain.
The graph now reads all accepted delta payloads in its project. These local
results do not establish current hosted CI, deployed acceptance or independent
human methods approval. Earlier proof records remain unchanged.
