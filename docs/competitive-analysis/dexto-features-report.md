# Dexto Feature Report — Candidates to Copy

**Source:** [truffle-ai/dexto](https://github.com/truffle-ai/dexto) — "an operating system for AI agents." Elastic License 2.0.
**Date:** 2026-06-07

---

## 1. What Dexto Is

Dexto positions itself as the layer between LLMs and applications: the LLM is the CPU, the context window is RAM, Dexto is the OS, and your agents are the apps. The whole system is **configuration-driven** — agents are defined in portable YAML, not code, so you can swap models and tools without touching source.

---

## 2. Feature Inventory

| # | Feature | What it does | Worth copying? |
|---|---------|--------------|----------------|
| 1 | **YAML agent definitions** | Agent behavior (LLM, MCP servers, system prompt, storage, permissions) lives in version-controlled YAML; edits hot-reload without losing state | ⭐ High |
| 2 | **Mid-conversation model switching** | `switchLLM()` swaps providers/models live, no restart | ⭐ High |
| 3 | **Multi-provider LLM support** | 50+ models: OpenAI, Anthropic, Gemini, Groq, xAI, plus gateways (OpenRouter, Bedrock, Vertex, LiteLLM) and local (Ollama, node-llama-cpp) | Medium |
| 4 | **MCP tool integration** | Connect 30+ tools (Puppeteer, Linear, ElevenLabs, Firecrawl, Sora) via stdio MCP servers; browse/test in Web UI | ⭐ High |
| 5 | **Human-in-the-loop permissions** | Manual-approve / auto-approve modes, per-tool allow/deny, per-session tracking, inherited approvals for sub-agents | ⭐ High |
| 6 | **Sub-agent spawning** | Parent delegates to ephemeral specialized agents; auto-cleanup, permission inheritance, concurrency/timeout limits | ⭐ High |
| 7 | **Session persistence** | Conversations survive restarts; resume by ID, cross-conversation search, configurable TTL/limits | ⭐ High |
| 8 | **Pluggable storage backends** | Cache: Redis / in-memory. DB: Postgres / SQLite / in-memory | Medium |
| 9 | **Multiple run modes** | Web UI, CLI, Web Server (REST + SSE), MCP server (stdio) — same agent, many surfaces | Medium |
| 10 | **Agent registry** | Pre-built installable agents (Coding, Podcast, Image Editor, Video, Database, GitHub, Triage); `dexto agents install [names]` | Medium |
| 11 | **Built-in coding agent** | Autonomous multi-file edit, shell/test execution, sub-agent exploration, persistent context | Low (you likely have own) |
| 12 | **Triage pattern** | A coordinator agent routes work to specialized agents | ⭐ High |
| 13 | **SDK (`@dexto/core`)** | Programmatic sessions, streaming, multimodal input, server instantiation | Medium |
| 14 | **Observability** | Logs to `~/.dexto/logs/`, MCP Playground for pre-deploy tool testing, opt-out telemetry | Medium |

---

## 3. Top Recommendations to Copy

These give the most leverage for the least integration cost:

### A. YAML-defined, hot-reloadable agents (#1)
Decouples agent behavior from code. Lets non-engineers tune prompts/tools, and makes agents portable + version-controlled. **This is the keystone feature** — most others (model switch, MCP config, permissions) hang off it.

### B. MCP tool integration (#4)
Standard protocol → instant access to a large ecosystem of tools without per-tool glue code. Web UI tool browser + a "playground" for testing before wiring into an agent.

### C. Human-in-the-loop permission framework (#5)
- Modes: manual approve, auto-approve.
- Granularity: per-tool allow/deny (e.g. `alwaysAllow: mcp--filesystem--read_file`), per-session.
- Sub-agents inherit parent approvals.
This is a safety primitive worth lifting almost verbatim.

### D. Ephemeral sub-agents + triage (#6, #12)
Parent agent spawns specialized children, they finish, auto-cleanup. Plus a triage/coordinator that routes. Good fit for breaking complex tasks into bounded contexts.

### E. Session persistence + resume + search (#7)
Conversations outlive process restarts; resume by ID; search across history. Big UX win for any long-running assistant.

---

## 4. Suggested Adoption Order

1. **Session persistence** (#7) — foundational, low risk, immediate UX value.
2. **YAML agent config** (#1) — the keystone; enables clean config of everything else.
3. **MCP integration** (#4) — unlocks tool ecosystem.
4. **Permission framework** (#5) — required before letting tools take real actions.
5. **Sub-agents + triage** (#6, #12) — once single-agent loop is solid.
6. **Multi-provider + live model switch** (#2, #3) — optimization layer, add when needed.

---

## 5. Watch-outs

- **License:** Dexto is **Elastic License 2.0** — not OSI-permissive. Copying *ideas/architecture* is fine; **lifting code verbatim** carries restrictions (can't offer it as a managed service to third parties). Reimplement, don't copy-paste.
- **Scope creep:** the full OS-for-agents framing is large. Adopt features individually, not the whole platform.
- **Storage backends (#8):** only worth it once you need horizontal scale; in-memory is fine to start.

---

*Report generated from the project README. For exact API shapes, inspect `@dexto/core` source and the YAML config schema directly.*
