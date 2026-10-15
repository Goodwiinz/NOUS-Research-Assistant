# Resuming a lapsed harness pairing without a new browser approval

**Status:** design, approved by the owner 2026-10-13. **Checked against:**
`origin/develop` at `9d5d8e869`, 2026-10-13. **Follows:** the 2026-10-13
amendment of `docs/plans/2026-10-12-nous-rc-design.md` (on `feat/nous-rc`),
which deferred "re-issuing a grant from an existing approval" to a backend
endpoint.

## Problem

An integration grant lives 15 minutes and `renew_grant` renews it only while it
is live. Only a running bridge renews, so a pairing lapses 3 to 15 minutes
after `nous-harness run` (or `nous --rc`) stops. The next `connect` for the
same project then runs the full flow: browser login, a new device and a new
browser consent. Approvals pile up under Connected devices, and the folders get
new IDs. The consent the user already approved is still valid
(`consent_revoked_at IS NULL`), but an outside client can only reach it through
`exchange_request`, which consumes a fresh request.

## Decisions (owner, 2026-10-13)

| Question | Decision |
| --- | --- |
| Authorization | Possession of the pairing's last grant token, plus the CLI JWT. Not a lookup by device ID. |
| Resume window | A grant may be resumed until 30 days after it expired (idle window). No absolute consent age cap, matching renewal and run mints. |
| `disconnect` on a lapsed pairing | Resume first, then revoke, so the consent is revoked server-side. |

Why not a lookup by device ID: CLI tokens are per user, live 30 days and are
not bound to a device, and `GET /integrations/devices` lists device IDs to any
CLI token. A device lookup would let any copy of a user's CLI token mint a
`harness:execute` grant for any approved device and silently stand in for that
computer's bridge (receive its runs), a step that today needs a browser
approval and shows a new device. With token possession only the holder of the
device's credential file can resume: the party that can already renew.

## Endpoint

`POST /api/v1/integrations/grants/{grant_id}/resume`, `require_cli_user`, with
`X-NOUS-Integration-Grant: <the stored grant token>`. No body. Returns
`IssuedGrant`. 403 (opaque) on any refusal, 409 when another resume or renewal
of the same grant won the race.

`resume_grant(db, user, grant_id, token)` in
`backend/src/services/integrations/context.py` refuses unless all hold:

1. The grant is the caller's (`owned_grant`: user and organization), the token
   has the grant-token shape and its SHA-256 equals `token_hash`
   (constant-time comparison), the grant is not deleted and `revoked_at IS NULL`.
   Renewal, CLI `disconnect`, Revoke under Connected devices and device
   disconnect all set `revoked_at`, so each of them stops resumes; a token
   renewed away cannot be replayed.
2. The grant has a device and a consent (`device_id`, `request_id`), and
   `expires_at` is at most 30 days in the past (`RESUME_WINDOW`). A live grant
   is accepted too; resuming it behaves like a renewal.
3. `check_scopes` and `validate_binding` pass again: project or workspace
   access, live user and organization, the chat still in the project, and the
   device owned and not revoked.
4. The consent is the grant's `request_id`, `consumed`, not revoked, not
   deleted, and has the grant's user, organization, project, workspace, device
   and chat; the grant's scopes are within the consent's.
5. A conditional update sets the old grant's `revoked_at` (409 if no row), and
   a new 15-minute grant is issued with the same binding, scopes, device, chat
   and consent. Scopes come from the old grant, so they never exceed the
   consent and there is no request body to widen them.

Run grants minted by `harness_streaming` discard their token and carry the
run's chat rather than a chat-bound consent's, so they cannot be resumed.

## Bridge

- `connect()` (`packages/harness-bridge/src/cli.ts`): on the reuse path (same
  API, binding and chat, every requested scope stored), when `grantIsLive`
  fails, call resume with the stored CLI token and grant token. On 200 write
  the new grant into the same credential handle and keep the device, folders
  and `connection.json`; print `Resumed binding: …`. On 409 re-read the store
  as renewal does. Any other answer (401 expired CLI token, 403 refusal, 404
  older server, network error) falls back to the full flow, as today.
- `disconnect()`: when the stored grant has expired, resume it, then revoke it
  with `DELETE /integrations/grants/{id}` as today. If resume is refused, keep
  today's behaviour (remove local credentials, point to Connected devices).
- `nous --rc` calls `connect()` after refreshing the stored CLI token, so it
  resumes through the same path.

## Out of scope

Grant lifetime. MCP children or a running bridge resuming by themselves after
a laptop sleeps (the same endpoint would serve it). Going back to an earlier
project's pairing after switching projects: its credential file is removed on
the switch, so that still needs a browser approval.

## Tests

Backend (SQLite, `test_context.py` style): the happy path and each refusal
(wrong or missing token, another user's grant, a token renewed away, consent
revoked, device disconnected, CLI disconnect, lost project access, chat moved
out of the project, scopes wider than the consent, beyond the window, no
device or consent). API: CLI auth required, header required, 403/409 mapping.
PostgreSQL (`ORCHESTRATION_TEST_DATABASE_URL`, skipped without it): two
concurrent resumes of one grant give one 200 and one 409, mutation-verified.
Bridge (fake fetch): an expired grant resumes with no `/cli-auth/start`, no
`/integrations/grant-requests` and no `POST /integrations/devices`; refusals
fall back to the full flow; `disconnect` on an expired grant resumes, then
revokes.
