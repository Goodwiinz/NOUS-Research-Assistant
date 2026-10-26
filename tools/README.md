# tools/

Standalone developer tooling that is not part of the application build.
Currently one package: [`nous-playwright/`](nous-playwright/README.md), an
isolated Playwright setup (Node 24, pnpm 10.18.2) with its own config, unit
tests and handoff notes. It is separate from the main end-to-end suite in
`tests/e2e/` and does not inherit that suite's seeded credentials or global
setup. See [`AGENTS.md`](AGENTS.md) for the editing rules.
