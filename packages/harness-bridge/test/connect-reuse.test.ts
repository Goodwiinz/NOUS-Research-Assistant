import assert from "node:assert/strict";
import { test } from "node:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { connect, handoffFlush, handoffShow, status } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const DEVICE = "22222222-2222-4222-8222-222222222222";
const GRANT = "33333333-3333-4333-8333-333333333333";
const WORKSPACE = "77777777-7777-4777-8777-777777777777";
const OTHER_WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
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
    // An exact reuse keeps nothing the command did not ask for.
    assert.doesNotMatch(t.messages.at(-1)!, /keeps scopes/);
    assert.equal(t.state().credentialHandle, first.credentialHandle);
    // Requesting a subset of the stored scopes is satisfied by the same binding
    // (the documented superset rule), and the notice names what the device
    // keeps and how to drop it (BR-3).
    assert.deepEqual(await connect({ ...t.base, threadId: CHAT, tools: true }), first);
    assert.match(
      t.messages.at(-1)!,
      /It keeps scopes this command did not request: handoff:read, handoff:write\. To drop them \(which also stops the device's runs\), revoke this device's access at \/integrations\/devices, or run nous-harness disconnect, then connect again; after disconnect, register its folders again with nous-harness workspace add\.$/,
    );
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

test("a workspace reuse names the scopes it keeps but not workspace add (it has no folders)", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    const workspace = { ...noProject, workspaceId: WORKSPACE, tools: true };
    await connect({ ...workspace, write: true });
    await connect(workspace);
    // A workspace connection runs nothing on the device; dropping scopes cuts its tools.
    assert.match(
      t.messages.at(-1)!,
      /It keeps scopes this command did not request: tools:write\. To drop them \(which also cuts this device's NOUS tools until it reconnects\), revoke this device's access at \/integrations\/devices, or run nous-harness disconnect, then connect again\.$/,
    );
    assert.doesNotMatch(t.messages.at(-1)!, /workspace add|stops the device's runs/);
  } finally {
    t.cleanup();
  }
});

test("a workspace binding is reused like a project one, and the two never reuse each other", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    const workspace = { ...noProject, workspaceId: WORKSPACE, tools: true };
    const first = await connect(workspace);
    assert.equal(t.state().workspaceId, WORKSPACE);
    assert.equal(t.state().projectId, undefined);
    // Same API and workspace, scopes already granted, grant still renews: one probe, no login.
    const sameMark = t.calls.length;
    assert.deepEqual(await connect(workspace), first);
    assert.deepEqual(t.since(sameMark), [`https://nous.test/api/v1/integrations/grants/${GRANT}/renew`]);
    assert.match(t.messages.at(-1)!, new RegExp(`^Reusing binding: workspace ${WORKSPACE}`));
    // A project connection on the same API is another binding: full flow, and the
    // workspace connection's local credential goes.
    const projectMark = t.calls.length;
    const project = await connect({ ...t.base, tools: true });
    assert.ok(t.since(projectMark).some((u) => u.endsWith("/cli-auth/start")));
    assert.equal(t.since(projectMark).some((u) => u.endsWith("/renew")), false);
    assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), false);
    assert.equal(t.state().projectId, PROJECT);
    assert.equal(t.state().workspaceId, undefined);
    // And back again: a workspace connection never reuses a project grant.
    const backMark = t.calls.length;
    await connect(workspace);
    assert.ok(t.since(backMark).some((u) => u.endsWith("/cli-auth/start")));
    assert.equal(existsSync(join(t.dir, `${project.credentialHandle}.json`)), false);
    assert.equal(t.state().workspaceId, WORKSPACE);
  } finally {
    t.cleanup();
  }
});

test("a different workspace runs the full flow and removes the superseded credential", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    const first = await connect({ ...noProject, workspaceId: WORKSPACE, tools: true });
    const mark = t.calls.length;
    const second = await connect({ ...noProject, workspaceId: OTHER_WORKSPACE, tools: true });
    const later = t.since(mark);
    // The workspace is the whole binding: a grant for another one must neither
    // be probed nor reused, whatever scopes it holds.
    assert.ok(later.some((u) => u.endsWith("/cli-auth/start")));
    assert.ok(later.some((u) => u.endsWith("/grant-requests")), "a new consent is requested");
    assert.equal(later.some((u) => u.endsWith("/renew")), false);
    assert.equal(t.messages.some((m) => m.startsWith("Reusing binding")), false);
    assert.notEqual(second.credentialHandle, first.credentialHandle);
    assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), false);
    assert.equal(existsSync(join(t.dir, `${second.credentialHandle}.json`)), true);
    assert.equal(t.state().workspaceId, OTHER_WORKSPACE);
    assert.equal(t.state().projectId, undefined);
  } finally {
    t.cleanup();
  }
});

test("a stored connection with both a project and a workspace, or with neither, is not connected", async () => {
  for (const [name, binding] of [
    ["both", { projectId: PROJECT, workspaceId: WORKSPACE }],
    ["neither", {}],
  ] as const) {
    const t = setup();
    try {
      await connect({ ...t.base, tools: true });
      const { projectId: _project, workspaceId: _workspace, ...rest } = t.state();
      await new CredentialStore(t.dir).writeLocal("connection", { ...rest, ...binding });
      const lines: string[] = [];
      await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
      const out = lines.join("\n");
      assert.match(out, /not connected/i, name);
      assert.doesNotMatch(out, /Project:|Workspace:|Device:/, name);
      await assert.rejects(handoffShow({ stateDir: t.dir, fetchFn: t.base.fetchFn }), /connect this device first/, name);
      // And it is never taken for the binding of the next connect.
      const mark = t.calls.length;
      await connect({ ...t.base, tools: true });
      assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")), name);
      assert.equal(t.since(mark).some((u) => u.endsWith("/renew")), false, name);
    } finally {
      t.cleanup();
    }
  }
});

test("status names a workspace binding and shows neither a project nor a chat", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    await connect({ ...noProject, workspaceId: WORKSPACE, tools: true });
    const lines: string[] = [];
    await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
    const out = lines.join("\n");
    assert.match(out, new RegExp(`Workspace: ${WORKSPACE} \\(every project in it; MCP-only\\)`));
    assert.doesNotMatch(out, /Project:|Chat:/);
    assert.match(out, /Scopes: tools:read/);
    assert.ok(!out.includes("secret"));
  } finally {
    t.cleanup();
  }
});

test("handoff commands refuse a workspace connection and say how to get a chat one", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    await connect({ ...noProject, workspaceId: WORKSPACE, tools: true });
    const mark = t.calls.length;
    const refusal = /workspace, which is MCP-only.*connect --project UUID --chat UUID --tools --handoff/;
    const options = { stateDir: t.dir, fetchFn: t.base.fetchFn };
    await assert.rejects(handoffShow(options), refusal);
    await assert.rejects(handoffFlush(options), refusal);
    assert.equal(t.calls.length, mark, "refused before any request");
  } finally {
    t.cleanup();
  }
});
