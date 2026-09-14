# AGENTS.md

Project guidelines for AI assistants.


<!-- tambo-docs-v1.0 -->
## Tambo AI Framework

This project uses **Tambo AI** for building AI assistants with generative UI and MCP support.

**Documentation**: https://docs.tambo.co/llms.txt

**CLI**: Use `npx tambo` to add UI components or upgrade. Run `npx tambo help` to learn more.

## Context7

When you need library/API documentation, code examples, or config steps, use the `context7` MCP tools to fetch up-to-date docs. Append `use context7` to prompts.

## Scope and sources of truth

This directory owns the Next.js application, frontend state, presentation
adapters, and browser-facing tests. Use these contracts before making a
frontend change:

- [Frontend standards](../docs/engineering/frontend.md)
- [API contracts](../docs/engineering/api-contracts.md)
- [Testing standards](../docs/engineering/testing.md)
- [Invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)
- [UX audit](../docs/frontend/ux-design-audit.md) and
  [WCAG checklist](../scripts/wcag-checklist.sh) as dated accessibility
  evidence, not proof of current compliance.

## Invalid patterns

- Give each server entity one client-side cache owner. TanStack Query owns
  request-backed entities; the chat Zustand store is the documented exception
  for the chat transcript. External callers import only the stable
  `@/store/chat-store` facade, never the internal `store/chat/*` slices.
- The backend remains the sole persisted-chat writer. Do not add a second
  create-message write in a hook or component. `chat-store`'s `refreshMessages`
  owns terminal reconciliation, including stale-request protection and waiting
  for the expected persisted/runtime message before settling freshness.
- Presentation code uses domain adapters rather than importing generated API
  types directly where an adapter exists. `frontend/src/types/generated/api.d.ts`
  is generated and never hand-edited; migrate HTTP shapes adopt-on-touch.
- Quality baselines and production exclusions only move stricter, in the same
  reviewed change that earns the move. Do not lower a floor or add an
  exclusion to make a changed-file check pass.
- Icon-only controls require accessible names. Keep keyboard and focus behavior
  operable, and show stable user-facing async errors without raw internal
  exception details.
- Node 24, `pnpm@10.18.2`, and the root `pnpm-lock.yaml` are authoritative.
  Do not introduce a nested JavaScript lockfile or an npm fallback.

## Required workflow

- Before adding state, identify the existing owner and avoid a second cache or
  shadow Server Action cache. Keep chat imports on `@/store/chat-store`, route
  composition free of service imports/business logic, and persistence in the
  backend path.
- When touching an HTTP request/response shape, adopt the generated schema
  through its domain adapter and regenerate the backend OpenAPI snapshot and
  frontend generated types together. Never edit generated output by hand.
- Preserve the ratchet baseline while making a change; if quality improves,
  tighten the committed baseline in that same reviewed change. Validate names,
  focus, and error disclosure for new interactive/async UI.
- Use the repository-pinned Node 24 toolchain with `pnpm@10.18.2` and the root
  lockfile so local and CI behavior remain aligned.

## Verification

Run the exact matrix-backed frontend checks:

```sh
pnpm --dir frontend lint:changed
pnpm --dir frontend quality:exclusions
pnpm --dir frontend type-check
pnpm --dir frontend test
```

Accessibility E2E is available as
`pnpm --dir frontend test:e2e:accessibility`, but it is browser/service-
dependent. Report it as `NOT RUN` when the required browser and running
application services are unavailable; do not imply that the unit checks prove
global accessibility compliance.
