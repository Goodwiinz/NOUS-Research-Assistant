# packages/

Shared TypeScript workspace packages, registered in the root
`pnpm-workspace.yaml` and installed by the single root `pnpm-lock.yaml`.

- [`chat-runtime/`](chat-runtime/README.md) (`@nous/chat-runtime`): chat message
  conversion and runtime adapter shared by the web app and the terminal client.
- [`harness-bridge/`](harness-bridge/README.md) (`@nous/harness-bridge`): the
  local bridge that connects a paired Codex CLI to a NOUS chat run.

Use `pnpm` only. Add a new package here only together with its
`pnpm-workspace.yaml` entry and a README.
