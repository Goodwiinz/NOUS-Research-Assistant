# NOUS CLI — Using with `dev-api.gen-text.app`

An Ink terminal chat client for the NOUS agent. It talks to a NOUS backend over HTTPS — auth via browser-based device flow, then streams agent responses (tokens + tool events + HITL confirmations) straight into your terminal.

Interactive `./nous` now launches Ink. See [the terminal guide](../../terminal/README.md) for native menus, keyboard controls, attachments, branches, and migrated commands. Login and one-shot queries continue to use the shared CLI services.

This guide targets the hosted dev API at **`https://dev-api.gen-text.app/`**.

---

## Prerequisites

- Node 24
- `pnpm@10.18.2`
- A NOUS account authorized on `dev-api.gen-text.app`

---

## One-time setup

```bash
# 1. Clone and install frontend deps
git clone https://github.com/goodwiins/rag.git nous
cd nous
pnpm install --frozen-lockfile

# 2. Point the CLI at the dev API
export NOUS_API_URL="https://dev-api.gen-text.app/api/v1"

# Make it permanent (pick your shell)
echo 'export NOUS_API_URL="https://dev-api.gen-text.app/api/v1"' >> ~/.zshrc   # or ~/.bashrc
```

The CLI reads `NOUS_API_URL` at startup. It **must** include the `/api/v1` suffix — it hits `POST {NOUS_API_URL}/cli-auth/start` and the streaming endpoint under the same base.

Optional: change where the token is stored (default `~/.nous/config.json`):

```bash
export NOUS_CONFIG_DIR="$HOME/.config/nous"
```

---

## Login

```bash
./nous login
```

What happens:

1. CLI calls `POST /cli-auth/start` and gets a session + browser URL.
2. Your default browser opens a consent page on `dev-api.gen-text.app`.
3. You sign in and click **Approve**.
4. CLI polls `/cli-auth/status/{session_id}` until it flips to `approved`.
5. Token, email, org ID, and expiry are written to `~/.nous/config.json`.

If the browser doesn't open automatically, the URL is printed — paste it manually.

### Re-auth

When a session expires, any command prints `Session expired. Run: ./nous login`. Just run `./nous login` again.

### Logout

Delete the config file:

```bash
rm ~/.nous/config.json
```

---

## Run the agent

### Interactive REPL

```bash
./nous
```

You get a prompt:

```
NOUS  ·  no thread

>
```

Type a question, hit Enter, and the agent streams back — tool calls appear as inline spinners, tokens stream in real time, and destructive actions pause for confirmation.

```
> find me three arxiv papers on retrieval-augmented generation
```

### One-shot (non-interactive)

Pass the query as arguments — useful for scripts and pipes:

```bash
./nous "summarize the latest paper on diffusion transformers"
./nous "export my citations as BibTeX" > refs.bib
```

---

## Slash commands

Available inside the REPL:

| Command                        | Purpose                                                 |
| ------------------------------ | ------------------------------------------------------- |
| `/help`                        | List commands                                           |
| `/new`                         | Start a fresh thread (clears conversation history)      |
| `/thread`                      | Show the current thread ID                              |
| `/context project <id> [name]` | Scope subsequent queries to a specific research project |
| `/context clear`               | Drop project scoping, go back to generic chat           |
| `/quit`                        | Exit the REPL (Ctrl+C also works)                       |

### Example session — project-scoped drafting

```
> /context project 3f7e8a2c-... "Diffusion Survey"
project: Diffusion Survey  ·  thread: 8a1b2c3d

> ingest the latest 5 papers on classifier-free guidance
✓ arxiv_search
⏸  ingest_papers (paused — awaiting confirmation)
  Ingest 5 papers into "Diffusion Survey"? [yes/no] yes
✓ ingest_papers

> draft a literature-review section on classifier-free guidance
✓ search_documents
✓ create_draft
(streaming draft with inline [cite:N] markers...)
```

---

## Human-in-the-loop confirmations

Destructive tools (document ingest, draft creation, note writes) pause via LangGraph `interrupt()`. The CLI surfaces them inline as `⏸ <tool> (paused — awaiting confirmation)` and requires an explicit `yes` or `no` followed by Enter. Approve → `Command(resume=True)` is sent and the agent continues. Reject → the tool call is discarded and the agent gets control back.

---

## Environment variables

| Variable          | Purpose                              | Default                        |
| ----------------- | ------------------------------------ | ------------------------------ |
| `NOUS_API_URL`    | Base API URL (must end in `/api/v1`) | `http://localhost:8000/api/v1` |
| `NOUS_CONFIG_DIR` | Where `config.json` lives            | `~/.nous`                      |

---

## Troubleshooting

| Symptom                              | Fix                                                                         |
| ------------------------------------ | --------------------------------------------------------------------------- |
| `Failed to start CLI auth: 404`      | Double-check `NOUS_API_URL` ends with `/api/v1` and the host is reachable   |
| `Not logged in. Run: ./nous login`   | First-time setup or config wiped — just run `./nous login`                  |
| `Session expired. Run: ./nous login` | Token past `expires_at` — re-login                                          |
| Browser didn't open                  | Copy the printed URL manually; login flow works the same way                |
| `Poll failed: 403`                   | Your account may not be authorized for this environment — contact the admin |
| Connection refused                   | `dev-api.gen-text.app` may be down; retry or check status                   |

### Quick connectivity check

```bash
curl -sS "$NOUS_API_URL/../health"
# or, explicitly:
curl -sS https://dev-api.gen-text.app/health
```

Should return `{"status":"ok"}` (or similar).

---

## Under the hood

- Entrypoint: [`frontend/cli/index.ts`](../../frontend/cli/index.ts)
- Auth flow: [`frontend/cli/auth/deviceFlow.ts`](../../frontend/cli/auth/deviceFlow.ts)
- Token store: [`frontend/cli/auth/store.ts`](../../frontend/cli/auth/store.ts)
- REPL + one-shot: [`frontend/cli/repl.ts`](../../frontend/cli/repl.ts)
- SSE streaming: [`frontend/cli/stream.ts`](../../frontend/cli/stream.ts)
- Wrapper script: [`nous`](../../nous) → `exec pnpm --prefix frontend cli "$@"`

### Endpoints the CLI calls

| Endpoint                        | Method | Purpose                       |
| ------------------------------- | ------ | ----------------------------- |
| `/cli-auth/start`               | POST   | Start device-flow session     |
| `/cli-auth/status/{session_id}` | GET    | Poll for approval             |
| `/agent/stream`                 | POST   | SSE stream of agent execution |
| `/agent/stream/confirm`       | POST   | Resume a HITL interrupt       |
