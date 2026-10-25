# NOUS CLI — Using with `dev-api.goodwiinz.tech`

An Ink terminal chat client for the NOUS agent. It talks to a NOUS backend over HTTPS — auth via browser-based device flow, then streams agent responses (tokens + tool events + HITL confirmations) straight into your terminal.

`./nous` launches the Ink terminal app, the only NOUS client CLI. See [the terminal guide](../../terminal/README.md) for native menus, keyboard controls, slash commands, attachments, and branches. There is no one-shot or non-interactive mode.

This guide targets the hosted dev API at **`https://dev-api.goodwiinz.tech/`**.

---

## Prerequisites

- Node 24
- `pnpm@10.18.2`
- A NOUS account authorized on `dev-api.goodwiinz.tech`

---

## One-time setup

```bash
# 1. Clone and install frontend deps
git clone https://github.com/Goodwiinz/NOUS-Research-Assistant.git nous
cd nous
pnpm install --frozen-lockfile

# 2. Point the CLI at the dev API
export NOUS_API_URL="https://dev-api.goodwiinz.tech/api/v1"

# Make it permanent (pick your shell)
echo 'export NOUS_API_URL="https://dev-api.goodwiinz.tech/api/v1"' >> ~/.zshrc   # or ~/.bashrc
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
2. Your default browser opens the consent page on the configured NOUS frontend.
3. You sign in, check the request details (IP, device, start time), type the code printed in your terminal, and click **Approve**. The link never contains the code. If someone sends you a sign-in link or a code, do not approve it.
4. CLI polls `/cli-auth/status/{session_id}` (poll token in the `X-CLI-Poll-Token` header, never the URL) until it flips to `approved`.
5. Token, email, org ID, and expiry are written to `~/.nous/config.json`.

If the browser doesn't open automatically, the URL is printed — paste it manually.

### Re-auth

When a session expires, `./nous` prints `Login required. Run ./nous login first.` Just run `./nous login` again.

### Logout

Delete the config file:

```bash
rm ~/.nous/config.json
```

---

## Run the agent

```bash
./nous          # resume the saved thread
./nous --new    # start a fresh thread
```

Type a question and press Enter; the agent streams back with tool activity inline. Destructive tools (document ingest, draft creation, note writes) pause via LangGraph `interrupt()` and wait for your approval before continuing. Slash commands, menus and keyboard controls are documented in [the terminal guide](../../terminal/README.md).

---

## Environment variables

| Variable          | Purpose                              | Default                        |
| ----------------- | ------------------------------------ | ------------------------------ |
| `NOUS_API_URL`    | Base API URL (must end in `/api/v1`) | `https://dev-api.goodwiinz.tech/api/v1` via `./nous` |
| `NOUS_CONFIG_DIR` | Where `config.json` lives            | `~/.nous`                      |

---

## Troubleshooting

| Symptom                              | Fix                                                                         |
| ------------------------------------ | --------------------------------------------------------------------------- |
| `Failed to start CLI auth: 404`      | Double-check `NOUS_API_URL` ends with `/api/v1` and the host is reachable   |
| `Login required. Run ./nous login first.` | First-time setup, config wiped, or token past `expires_at` — run `./nous login` |
| `Ink requires an interactive terminal.`   | `./nous` was run without a TTY (pipe, script, CI); run it in a terminal     |
| Browser didn't open                  | Copy the printed URL manually; login flow works the same way                |
| `Poll failed: 403`                   | Your account may not be authorized for this environment — contact the admin |
| Connection refused                   | `dev-api.goodwiinz.tech` may be down; retry or check status                   |

### Quick connectivity check

```bash
curl -sS "$NOUS_API_URL/../health"
# or, explicitly:
curl -sS https://dev-api.goodwiinz.tech/health
```

Should return `{"status":"ok"}` (or similar).

---

## Under the hood

- Entrypoint: [`terminal/src/index.tsx`](../../terminal/src/index.tsx)
- Auth flow: [`frontend/cli/auth/deviceFlow.ts`](../../frontend/cli/auth/deviceFlow.ts)
- Token store: [`frontend/cli/auth/store.ts`](../../frontend/cli/auth/store.ts)
- SSE streaming: [`frontend/cli/stream.ts`](../../frontend/cli/stream.ts)
- Wrapper script: [`nous`](../../nous) → `exec pnpm --prefix terminal start "$@"`

### Endpoints the CLI calls

| Endpoint                        | Method | Purpose                       |
| ------------------------------- | ------ | ----------------------------- |
| `/cli-auth/start`               | POST   | Start device-flow session     |
| `/cli-auth/status/{session_id}` | GET    | Poll for approval             |
| `/agent/stream`                 | POST   | SSE stream of agent execution |
| `/agent/stream/confirm`       | POST   | Resume a HITL interrupt       |
