# Research protocols and retained decisions

Current implementation contract for GOO-295 and GOO-296. The canonical project
identifier remains `Collection.id`; the research-engine bridge and all legacy
identifiers remain unchanged. See [backend standards](backend.md) and
[API contracts](api-contracts.md) for the surrounding boundaries.

## Separate meanings

- **Approved** means an explicitly assigned supervisor accepted an exact local
  protocol version. The supervisor must differ from that version's author.
- **Registered** means a user recorded an external acknowledgment and receipt for
  that exact version/hash. This implementation records receipts; it does not
  submit to or independently verify an external registry. A failed registration
  record cannot revoke local approval.
- **Plan verified** means a pending/running execution matches the approved plan.
  **Conformant** describes a completed execution of that plan, not evidence
  correctness, exhaustive search, or scientific validity. A recorded departure
  makes the affected run **deviated** and prevents unattended resume.

A named methods reviewer and selected pilot review type/domain remain required
for scientific acceptance. Engineering tests do not substitute for that review.

## Version and approval behavior

Question and protocol identities are stable; their content versions are
append-only. Protocol content binds an exact question version, seven methods
sections, and the full execution blueprint snapshot (ID, integer version, steps,
parameters). A new blueprint revision cannot silently change an approved plan.

| Operation | Required authority | Result |
| --- | --- | --- |
| Create question or protocol version | Workspace content editor within project boundary | New immutable draft content |
| Create amendment | Content editor, explicit predecessor and reason | New draft; previous approval remains current |
| Approve current draft | Independent explicitly assigned supervisor | Exact draft becomes approved; previous approval becomes superseded |
| Read private history | Current project membership and source/organization boundary | Authorized domain projection |
| Start a run | Content editor and current approved version | Pending run bound to exact plan/hash |
| Resume existing run | Content editor and intact original approved binding | Original plan, even if later superseded |

Version states are `draft`, `approved`, `superseded`. Amendment is lineage and a
change kind, not a fourth state. Approved content is read-only in the UI.

Approval checks the expected version number, content hash and current approved
pointer. The protocol state and scientific decision events commit together.
The ledger primitive flushes but never commits or rolls back its caller's
transaction. Approval retries are scoped to one protocol aggregate: the same
key and fingerprint identifies the original decision; conflicting reuse fails.
Fingerprints include the actor, target, expected state and rationale.

## Canonical content hashes

`research-protocol-v1` hashes UTF-8 JSON with SHA-256. Keys are sorted, separators
are comma/colon without extra whitespace, Unicode is retained, and array order
is significant. Non-finite numbers are rejected. Numeric zero normalizes to
integer zero; integral floats normalize to integers so PostgreSQL JSONB
round-trips preserve the digest. UUID references are lowercase UUID strings.
Absent optional question fields are represented explicitly as null.

The protocol digest includes the canonicalization version, exact question
version, blueprint identity, methods snapshot and captured execution plan. The
run's effective-plan digest binds this protocol digest to its execution plan.
Hashes establish content identity; they do not themselves establish authority.

## Authorization and locking

Lock order is Workspace `FOR SHARE`, Collection `FOR UPDATE`, domain aggregate,
domain versions, decision stream. Membership and workspace lifecycle writers
use Workspace `FOR UPDATE`; scientific role writers use the Collection lock.
After any wait, the resolver reloads workspace/collection lifecycle, membership,
organization scope and scientific roles. Locked domain queries also refresh the
SQLAlchemy identity map: acquiring a database lock alone does not reload a
previously fetched protocol pointer. Locks last until caller commit.
Different collections can share the workspace read lock. A membership revocation
cannot commit between approval's authorization check and approval commit.

Public workspace visibility does not expose these private protocol/decision
records. New internal tables enable RLS and revoke direct anonymous and
`authenticated` Data API privileges. Domain APIs retain the shared resolver;
there is no unrestricted generic event-dump endpoint.

## Execution, history and rollout

New runs require an explicit approved protocol version. Omitting the optional
compatibility request field produces `409 approved_protocol_required`.
The current engine exposes no approved operational override contract, so all
non-empty arbitrary parameter overrides require an amendment. Stream and resume
verify the binding before provider admission and execute the saved plan.

Pre-existing runs keep their identifiers and history and receive null protocol
and plan-hash fields with `legacy_unbound` status. They remain readable; execution
requires creating a new run from an approved plan. Migration never invents an
approval or actor.

Version, decision, registration and deviation rows have database mutation guards.
A deviation output reference is a retained research-step UUID bound to its
explicit run and original protocol; arbitrary strings cannot assert provenance. Retention-sensitive
foreign keys use `RESTRICT`; agent-run deletion cannot remove scientific
history. Ledger ordering is allocated under a per-aggregate stream lock.
Replay rejects unknown event schemas, missing subject versions, hash mismatch,
sequence gaps and contradictory approval/supersession transitions.

The protocol tables follow ledger migration `a3c5e7f901b2` with
`b4d6f8021a3c`; the prior head is `x6y7z8a9b0c1`.

## Verification evidence

The focused PostgreSQL suites use isolated schemas and independent sessions.
`test_research_authorization_concurrency.py` observes `pg_blocking_pids` to prove
both revocation-first and approval-first orderings. Mutation verification moved
authorization back before the locks: both revocation tests failed with
`DID NOT RAISE HTTPException`; restoring the guard passed all four tests.

The ledger concurrency mutation removes its stream row lock. Its retry test
then fails with a duplicate `(stream_id, seq)` constraint violation; restoring
the lock passes. See the ledger and protocol integration tests for atomic
rollback, retained rows and replay checks. Browser harness results must be
reported separately from production authentication and live acceptance.

The run-conformance mutations remove blueprint drift comparison and persisted
override rejection independently. Each corresponding PostgreSQL test then
fails; restoring both guards passes. Replay also rejects a supersession target
hash that disagrees with the following approval, with the same mutation proof.

The approval-versus-amendment test holds the collection lock until the waiting
approval has cached the old draft, then commits a replacement. Removing the
locked aggregate refresh wrongly returns approval success; restoring it
returns 409 without a decision event. A stale cached blueprint is likewise
reloaded under the project lock before execution-plan comparison.
