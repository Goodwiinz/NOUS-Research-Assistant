# Pinned request schemas

These three JSON Schema files are unmodified output from the installed
`codex-cli 0.153.4` command:

```sh
codex --version
codex app-server generate-json-schema --out <temporary-directory>
```

The adapter validates native requests against these schemas with Ajv, then
applies its narrower session, callback, and one-time approval restrictions.
It supports only `0.153.4`; regenerate and review schemas and subprocess tests
before changing that pin. The generated TypeScript definitions were also
inspected for the start/resume effective sandbox and turn/event wire shapes.
