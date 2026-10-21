# API contracts

FastAPI/Pydantic is the HTTP source of truth. Nothing downstream is
hand-typed against a guess of the wire shape — it's generated from the app
itself.

The additive [alphaXiv PDF chat adapter](alphaxiv-compatibility.md) documents
its supported v2/v3 requests, branch history, native identity mapping, and remaining
compatibility limits.

## Pipeline

1. `python scripts/ci/generate_openapi.py` rebuilds `backend/openapi.json`
   by constructing the FastAPI app in-memory (SQLite, placeholder Azure
   creds — no DB/Redis/network) and calling `app.openapi()`.
2. `pnpm --dir frontend generate:api-types` (openapi-typescript) rebuilds
   `frontend/src/types/generated/api.d.ts` from that committed snapshot.
3. CI's `openapi-contract` job blocks on **both** halves drifting:
   `python scripts/ci/generate_openapi.py --check` (backend schema vs. the
   running app), then it regenerates the TypeScript and runs
   `git diff --exit-code -- backend/openapi.json
   frontend/src/types/generated/api.d.ts`. A schema change and its
   regenerated types must land in the same PR — commit both together.

## Compatibility gate

The freshness diff above is blind to *compatibility*: a breaking change that
regenerates both committed artifacts passes it. On pull requests the
`openapi-contract` job also runs [oasdiff](https://github.com/oasdiff/oasdiff)
against the PR base's committed spec:

- **Blocks:** `oasdiff breaking --fail-on ERR` — any **ERR-level** breaking
  change (removed path/operation, removed required response field, a new
  required request param, a narrowed type, etc.) fails the job. WARN-level
  changes do not block.
- **Informational:** `oasdiff changelog` writes the full change list to the job
  summary on every PR — never blocking.
- **Skips cleanly** on non-PR events and when there is no base spec to compare
  (new file / empty base sha) — it exits 0 with a summary note, never fails.

### Escape hatch

An intentional, reviewed breaking change lands by adding the
**`api-breaking-approved`** label to the PR — the blocking gate then skips (the
changelog step still records what changed). Legitimate uses: shipping a
deliberate v-next contract change with its consumers updated in the same PR, or
removing an endpoint already confirmed dead. It is not a way to defer fixing an
accidental break — drop the label and fix the diff instead.

### Bumping the pinned oasdiff

The binary is pinned by version **and** tarball sha256 in the `openapi-contract`
job (`OASDIFF_VERSION` / `OASDIFF_SHA256`). To bump, take both from the release's
`checksums.txt` at <https://github.com/oasdiff/oasdiff/releases> and update them
together — never `go install` at CI time, never pipe curl to a shell.

## Adopt-on-touch

- Alias generated shapes from `components['schemas'][...]`; don't re-type
  them by hand. `frontend/src/types/api/workspace-contract.ts` is the
  worked example for the workspace resource hierarchy.
- Migration is **per-file, on touch** — editing a hand-written type that
  owns an HTTP request/response shape is the trigger to alias it from the
  generated schema, not a mandate to migrate every type at once.
  `frontend/src/types/README.md` lists current adopters
  (`services/documentAnalyticsApi.ts`, `services/workspaceService.ts`).
- Where a generated field is looser than the frontend needs (a JSONB column
  the backend deliberately leaves as an untyped passthrough, so it generates
  as `Record<string, unknown>[]`) or stricter than
  the wire contract actually requires (`openapi-typescript`'s
  `defaultNonNullable` marks any Pydantic-defaulted field as required, even
  though a caller may still omit it), narrow or relax it with an explicit
  derived type in the adopting module. Never paper over a mismatch with
  `as unknown as` — that's exactly the drift this pipeline exists to catch.
- Frontend-only concepts (streaming frames, planner steps, optimistic ids,
  UI enums, view models) stay hand-written; only wire request/response
  shapes are candidates for aliasing. Note: JSONB itself is not the reason a
  type stays hand-written — a typed JSONB column can be modeled in Pydantic
  and flows through the generated schema like any other field. Hand-written
  types are reserved for intentionally-opaque JSON (where the backend
  deliberately declines to type the column) and view-model shapes, not for
  "JSONB can't be typed".

## Commands

```sh
python scripts/ci/generate_openapi.py            # refresh backend/openapi.json
python scripts/ci/generate_openapi.py --check     # verify no drift
pnpm --dir frontend generate:api-types            # rebuild generated/api.d.ts
pnpm --dir frontend check:api-types               # regenerate + diff both in one step
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
```
