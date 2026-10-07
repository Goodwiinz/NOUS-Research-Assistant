import assert from "node:assert/strict";
import { test } from "node:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { connect, status } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const DEVICE = "22222222-2222-4222-8222-222222222222";
const GRANT = "33333333-3333-4333-8333-333333333333";
const CHAT = "99999999-9999-4999-8999-999999999999";
const OTHER_CHAT = "88888888-8888-4888-8888-888888888888";

type Reply = { code: number; body: object };

/** Fake NOUS: full login + consent, and a renew endpoint whose answer each test picks. */
function fakeNous(calls: string[], renew: () => Reply): typeof fetch {
  let issued = 0;
  return (async (input: any) => {
    const url = String(input);
    calls.push(url);
    let data: object;
    if (url.endsWith("/cli-auth/start"))
      data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l", verification_code: "ABCD-1234" };
    else if (url.includes("/cli-auth/status/")) data = { status: "approved", token: "cli-secret" };
    else if (url.endsWith("/integrations/devices")) data = { id: DEVICE };
    else if (url.endsWith("/grant-requests")) data = { id: GRANT, approval_url: "https://nous.test/a" };
    else if (url.endsWith("/exchange")) data = { token: `grant-secret-${(issued += 1)}`, grant_id: GRANT };
    else if (url.endsWith("/renew")) {
      const { code, body } = renew();
      return new Response(JSON.stringify(body), { status: code });
    } else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
}

let renewals = 0;
const renewed = (): Reply => ({ code: 200, body: { token: `renewed-secret-${(renewals += 1)}`, grant_id: GRANT } });

function setup(renew: () => Reply = renewed) {
  const dir = mkdtempSync(join(tmpdir(), "nous-reuse-"));
  const calls: string[] = [];
  const messages: string[] = [];
  const base = {
    stateDir: dir,
    fetchFn: fakeNous(calls, renew),
    announce: (m: string) => messages.push(m),
    apiUrl: "https://nous.test/api/v1",
    projectId: PROJECT,
    label: "Laptop",
  };
  return {
    dir,
    calls,
    messages,
    base,
    state: () => JSON.parse(readFileSync(join(dir, "connection.json"), "utf8")),
    since: (mark: number) => calls.slice(mark),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

test("same binding with a live grant reuses it without login or consent", async () => {
  const t = setup();
  try {
    const first = await connect({ ...t.base, threadId: CHAT, tools: true, handoff: true });
    const mark = t.calls.length;
    const again = await connect({ ...t.base, threadId: CHAT, tools: true, handoff: true });
    assert.deepEqual(again, first);
    // The liveness probe is one forced renewal; no /cli-auth/start, no consent.
    assert.deepEqual(t.since(mark), [`https://nous.test/api/v1/integrations/grants/${GRANT}/renew`]);
    assert.match(t.messages.at(-1)!, /^Reusing binding/);
    assert.equal(t.state().credentialHandle, first.credentialHandle);
    // Requesting a subset of the stored scopes is satisfied by the same binding.
    assert.deepEqual(await connect({ ...t.base, threadId: CHAT, tools: true }), first);
    assert.ok(!t.messages.join("\n").includes("secret"));
  } finally {
    t.cleanup();
  }
});

test("a different chat runs the full flow and removes the superseded credential", async () => {
  const t = setup();
  try {
    const first = await connect({ ...t.base, threadId: CHAT });
    const mark = t.calls.length;
    const second = await connect({ ...t.base, threadId: OTHER_CHAT });
    assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")));
    assert.equal(t.since(mark).some((u) => u.endsWith("/renew")), false);
    assert.notEqual(second.credentialHandle, first.credentialHandle);
    assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), false);
    assert.equal(existsSync(join(t.dir, `${second.credentialHandle}.json`)), true);
    assert.equal(t.state().threadId, OTHER_CHAT);
    // Unbinding (no --chat) is a different binding too.
    const third = await connect(t.base);
    assert.equal(existsSync(join(t.dir, `${second.credentialHandle}.json`)), false);
    assert.equal(t.state().threadId, undefined);
    assert.equal(t.state().credentialHandle, third.credentialHandle);
  } finally {
    t.cleanup();
  }
});

test("a failed liveness probe falls back to the full login and consent flow", async () => {
  for (const reply of [
    { code: 403, body: { detail: "revoked" } },
    // A transient renewal failure keeps the old token: not proof of liveness.
    { code: 503, body: { detail: "down" } },
  ]) {
    const t = setup(() => reply);
    try {
      const first = await connect({ ...t.base, threadId: CHAT });
      const mark = t.calls.length;
      const second = await connect({ ...t.base, threadId: CHAT });
      const later = t.since(mark);
      assert.ok(later.some((u) => u.endsWith("/renew")));
      assert.ok(later.some((u) => u.endsWith("/cli-auth/start")), `full flow after ${reply.code}`);
      assert.notEqual(second.credentialHandle, first.credentialHandle);
      assert.equal(t.messages.some((m) => m.startsWith("Reusing binding")), false);
    } finally {
      t.cleanup();
    }
  }
});

test("a requested scope missing from the stored binding runs the full flow", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, threadId: CHAT, tools: true });
    const mark = t.calls.length;
    await connect({ ...t.base, threadId: CHAT, tools: true, handoff: true });
    const later = t.since(mark);
    // No probe is spent when the scopes already rule reuse out.
    assert.equal(later.some((u) => u.endsWith("/renew")), false);
    assert.ok(later.some((u) => u.endsWith("/cli-auth/start")));
    assert.ok(t.state().scopes.includes("handoff:write"));
  } finally {
    t.cleanup();
  }
});

test("a connection without a stored grant id is never reused", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, threadId: CHAT });
    await new CredentialStore(t.dir).update(t.state().credentialHandle, {
      accessToken: "cli-secret",
      grantToken: "legacy",
    });
    const mark = t.calls.length;
    await connect({ ...t.base, threadId: CHAT });
    assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")));
  } finally {
    t.cleanup();
  }
});

test("status reports the binding offline, and says not connected without state", async () => {
  const t = setup();
  try {
    const lines: string[] = [];
    await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
    assert.match(lines.join("\n"), /not connected/i);
    const first = await connect({ ...t.base, threadId: CHAT, tools: true, handoff: true });
    const store = new CredentialStore(t.dir);
    for (const [id, thread, state] of [
      ["44444444-4444-4444-8444-444444444444", CHAT, "pending"],
      ["55555555-5555-4555-8555-555555555555", OTHER_CHAT, "pending"],
      ["66666666-6666-4666-8666-666666666666", CHAT, "rejected"],
    ])
      await store.writeLocal(`handoff-${id}`, {
        handoff_id: id,
        thread_id: thread,
        project_id: PROJECT,
        body: { handoff_id: id, expected_parent_version: null, goal: "g", harness_name: "x" },
        state,
        created_at: new Date().toISOString(),
      });
    const mark = t.calls.length;
    lines.length = 0;
    await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
    const out = lines.join("\n");
    assert.equal(t.calls.length, mark, "status makes no network calls");
    assert.match(out, new RegExp(`Project: ${PROJECT}`));
    assert.match(out, new RegExp(`Chat: ${CHAT}`));
    assert.match(out, new RegExp(`Device: Laptop \\(${first.deviceId}\\)`));
    assert.match(out, /Grant: expires \d{4}-\d{2}-\d{2}T/);
    assert.match(out, /Pending handoffs: 2/);
    assert.ok(!out.includes("secret"));
  } finally {
    t.cleanup();
  }
});
