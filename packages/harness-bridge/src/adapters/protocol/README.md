# Pinned request schemas

These three JSON Schema files are unmodified output of

```sh
codex app-server generate-json-schema --out <temporary-directory>
```

for every Codex version in `SUPPORTED_CODEX_VERSIONS` (`../codex.ts`). The
adapter validates native requests against them with Ajv, then applies its
narrower session, callback and one-time approval restrictions.

## Tested versions

| Codex | Added | Schemas | Live run from a NOUS chat |
| --- | --- | --- | --- |
| `0.153.4` | 2026-09-28 | these files | 2026-10-05, local patched backend: chat runs completed; the approval scenario NOT RUN (`docs/testing/evidence/harness-live-proof-20261005/`) |
| `0.161.0` | 2026-10-12 | identical to `0.153.4` | NOT RUN |

## Adding a version

1. Install the new Codex and run the command above into a temporary folder.
2. Compare its `CommandExecutionRequestApprovalParams.json`,
   `FileChangeRequestApprovalParams.json` and `ToolRequestUserInputParams.json`
   with these files. If they differ, the adapter's validation changes and
   `test/codex.test.ts` must cover the difference before the version is listed.
3. Compare `codex_app_server_protocol.v2.schemas.json` with the previous
   version's for the methods the adapter uses (`initialize`, `thread/start`,
   `thread/resume`, `thread/read`, `turn/start`, `turn/interrupt`,
   `serverRequest/resolved` and the `item/*`, `turn/*`, `thread/*`
   notifications). Added optional fields are fine; a removed, renamed or newly
   required field *that the adapter sends or reads* needs an adapter change;
   record any other removal in the PR. The previous version's output is not
   vendored; regenerate it with that Codex (for example
   `npx @openai/codex@<previous> app-server generate-json-schema --out <folder>`).
4. On a branch, add the version to `SUPPORTED_CODEX_VERSIONS` (the bridge
   refuses unlisted versions, so the live run needs it listed) and run
   `pnpm --filter @nous/harness-bridge test`.
5. From that branch, do one live run from a NOUS chat with that Codex: a turn
   with streamed output and one command approval. Ask for a write OUTSIDE the
   registered folder: under `workspace-write` with `on-request`, a write inside
   it never asks for approval. A green test suite alone is
   not evidence: on 2026-10-05 a reply shape the fixture did not model
   (`writableRoots`) broke every run while the suite passed.
6. Record the evidence location in the table above. A row that still says
   `NOT RUN` may exist only on an unmerged branch: do not merge the PR that
   lists a version until its live run is recorded.
