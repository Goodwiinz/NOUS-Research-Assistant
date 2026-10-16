# Selected context, chat binding and handoff evidence (2026-10-16)

Scope: GOO-430 (DV-3, HO-3, AD-2), within GOO-419. HO-4/GOO-423 is a separate
organization-key proposal; this record does not close the parent audit.

Base: `4c4600392c0e4a3705f58b4abcdf6868c8e226bc`. Branch
`codex/goo-430-audit-fixes`, managed worktree `linear-audit-fixes/RAG_system`.
The existing eleven commits ending at `f3349be91` were reused. The original
worktree and branch were preserved; two porting conflicts retained the newer
CLI commands and current discovery timeout documentation.

## Verified behavior

- Selected memories page within the complete 64 KiB response envelope. Save
  refuses only a memory that cannot fit whole beside the frozen skill catalog;
  later reads use stable saved-list offsets. The API and MCP forward bounded
  offsets from 0 through 25. Contracts were regenerated together.
- A chat's live project links retain its device binding after an additive
  relink. Organization, workspace, membership and deletion predicates remain.
- A readable chat without a handoff returns 200 null; an inaccessible chat
  retains 404. The access check loads no members and uses one SELECT. Mounted
  views poll every five minutes while empty and every thirty seconds while
  active; background polling stays disabled.
- The browser shows the newest accepted save from any live linked project in
  the chat workspace. Save time orders across project chains; harness reads,
  replay and optimistic concurrency retain separate project chains. Deleted
  links/projects and moved projects are excluded. New browser-started runs
  still require the current-project device binding, as the CLI/docs state.

## Checks and causal failures

The final owning backend matrix passed **212 tests**. The final focused
handoff/binding/route matrix also passed 91 tests; three new linked-project
cases first failed by returning older Q3 instead of newer P1. The affected frontend views passed 20 tests. The original full bridge
suite passed 314 tests and the final MCP/context subset passed 34. Every
blocking local gate passed with Node 24.21.0, pnpm 10.18.2 and the pinned
minimal Python 3.11 lint environment. The unchanged migration-delta and
empty-chain probes skipped; inherited full-tree ESLint debt remains advisory.

```sh
scripts/ci/run_local_ci.sh --base origin/develop --frontend --skip-tests
pnpm --dir frontend exec vitest run src/components/chat/artifact-panel/__tests__/HandoffCard.test.tsx src/components/integrations/__tests__/ContextSelection.test.tsx
pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test test/mcp.test.ts test/context.test.ts
```

The first-save retry in `selected_context.py` was mutation-verified: replacing
its IntegrityError catch with a non-matching exception made
`test_concurrent_first_save_applies_instead_of_failing` fail on the expected
unique collision, then pass after byte-identical restoration. Source SHA-256:
`4a34cc6196c6e3e09d7510c7a0ed3fab2e30230a997df863242a2e4aa1110633`.

The browser read's deleted-link, deleted-project, workspace and time-ordering
guards in `handoffs.py` were each removed in turn. The focused
`test_browser_sees_newest_save_from_live_linked_projects` cases failed on the
corresponding behavioral error, then all three passed after exact restoration.
Restored source SHA-256:
`424ddd4219dc42b8cfe39b6f5fbf0fbf52498a362a1cbab1b4798fc26f25ddbc`.
Focused command for either proof: `PYTHONPATH=backend python -m pytest -c
backend/pytest.ini <owning-test-file> -k <test-name> -q`.

## Independent review and limits

Claude Fable correctness session `c5d1b568-a65a-46a6-b5e7-7427251ee30e` and
separate security session `333824f9-e418-4948-aae9-68bfbb07ad4c` both identified
the linked-project browser visibility gap on their first pass. Both final
continuations found no remaining concrete source defects; both read-only
tripwires were false. Reviewed source fingerprints match the delivered files.

Backend execution used the existing Python 3.11.13 runtime with FastAPI
0.138.1, Pydantic 2.13.4 and SQLAlchemy 2.0.51, which differ from CI's runtime
requirements. Static gates used CI's pinned linters independently. The tests
exercise local fixtures and source; they do not establish authenticated
deployed acceptance. No deployment, live flag change, merge or manual Linear
status change was performed. Hosted CI must be checked on the published head.
