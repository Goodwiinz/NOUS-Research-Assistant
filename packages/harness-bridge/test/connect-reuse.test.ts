import assert from "node:assert/strict";
import { mock, test } from "node:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { connect, handoffFlush, handoffShow, login, status } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const DEVICE = "22222222-2222-4222-8222-222222222222";
const GRANT = "33333333-3333-4333-8333-333333333333";
const RESUMED = "44444444-4444-4444-8444-444444444444";
const WORKSPACE = "77777777-7777-4777-8777-777777777777";
const OTHER_WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CHAT = "99999999-9999-4999-8999-999999999999";
const OTHER_CHAT = "88888888-8888-4888-8888-888888888888";

type Reply = { code: number; body: object };

/** NOUS before grant resume shipped: the route does not exist. */
const noResume = (): Reply => ({ code: 404, body: { detail: "Not Found" } });

/** Fake NOUS: full login + consent, and renew and resume endpoints whose answers each test picks. */
function fakeNous(
  calls: string[],
  renew: () => Reply,
  deviceAuth: (string | null)[] = [],
  resume: () => Reply = noResume,
): typeof fetch {
  let issued = 0;
  return (async (input: any, init?: RequestInit) => {
    const url = String(input);
    calls.push(url);
    let data: object;
    if (url.endsWith("/cli-auth/start"))
      data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l", verification_code: "ABCD-1234" };
    else if (url.includes("/cli-auth/status/")) data = { status: "approved", token: "cli-secret" };
    else if (url.endsWith("/integrations/devices")) {
      deviceAuth.push(new Headers(init?.headers).get("Authorization"));
      data = { id: DEVICE };
    }
    // NOUS sends the approval page root-relative (integrations/context.py).
    else if (url.endsWith("/grant-requests")) data = { id: GRANT, approval_url: `/integrations/approve?request_id=${GRANT}` };
    else if (url.endsWith("/exchange")) data = { token: `grant-secret-${(issued += 1)}`, grant_id: GRANT };
    else if (url.endsWith("/renew") || url.endsWith("/resume")) {
      const { code, body } = (url.endsWith("/renew") ? renew : resume)();
      return new Response(JSON.stringify(body), { status: code });
    } else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
}

let renewals = 0;
const renewed = (): Reply => ({ code: 200, body: { token: `renewed-secret-${(renewals += 1)}`, grant_id: GRANT } });

function setup(renew: () => Reply = renewed, resume: () => Reply = noResume) {
  const dir = mkdtempSync(join(tmpdir(), "nous-reuse-"));
  const calls: string[] = [];
  const messages: string[] = [];
  const deviceAuth: (string | null)[] = [];
  const base = {
    stateDir: dir,
    fetchFn: fakeNous(calls, renew, deviceAuth, resume),
    announce: (m: string) => messages.push(m),
    apiUrl: "https://nous.test/api/v1",
    projectId: PROJECT,
    label: "Laptop",
  };
  return {
    dir,
    calls,
    messages,
    deviceAuth,
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
    assert.equal(t.since(mark).some((u) => u.endsWith("/renew") || u.endsWith("/resume")), false);
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
      assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), false, `superseded credential removed after ${reply.code}`);
      assert.equal(t.state().credentialHandle, second.credentialHandle);
      assert.equal(t.messages.some((m) => m.startsWith("Reusing binding")), false);
    } finally {
      t.cleanup();
    }
  }
});

/** A pairing whose grant lapsed after `connect`: renewal refused, resume answered by `resume`. */
function lapsing(resume: () => Reply) {
  let lapsed = false;
  const t = setup(() => (lapsed ? { code: 403, body: { detail: "expired" } } : renewed()), resume);
  return { ...t, lapse: () => (lapsed = true) };
}
const resumedGrant = (): Reply => ({ code: 200, body: { token: "resumed-secret", grant_id: RESUMED } });

test("a lapsed grant resumes from its consent with no login, consent or new device", async () => {
  const t = lapsing(resumedGrant);
  try {
    const first = await connect({ ...t.base, tools: true });
    const before = t.state();
    t.lapse();
    const mark = t.calls.length;
    const again = await connect({ ...t.base, tools: true });
    assert.deepEqual(again, first);
    // One failed liveness probe, then one resume of the stored grant: no
    // /cli-auth/start, no /integrations/grant-requests, no new device.
    assert.deepEqual(t.since(mark), [
      `https://nous.test/api/v1/integrations/grants/${GRANT}/renew`,
      `https://nous.test/api/v1/integrations/grants/${GRANT}/resume`,
    ]);
    assert.deepEqual(t.state(), before, "same device, folders and credential handle");
    const stored = await new CredentialStore(t.dir).load(first.credentialHandle);
    assert.equal(stored.grantToken, "resumed-secret");
    assert.equal(stored.grantId, RESUMED);
    assert.equal(stored.accessToken, "cli-secret");
    assert.match(
      t.messages.at(-1)!,
      new RegExp(`^Resumed binding: project ${PROJECT}; your earlier approval still holds, so no new login or consent was needed\\.$`),
    );
    assert.ok(!t.messages.join("\n").includes("secret"));
  } finally {
    t.cleanup();
  }
});

test("a resumed binding names the scopes it keeps, like a reused one", async () => {
  const t = lapsing(resumedGrant);
  try {
    await connect({ ...t.base, threadId: CHAT, tools: true, handoff: true });
    t.lapse();
    await connect({ ...t.base, threadId: CHAT, tools: true });
    assert.match(t.messages.at(-1)!, new RegExp(`^Resumed binding: project ${PROJECT}, chat ${CHAT};`));
    assert.match(t.messages.at(-1)!, /It keeps scopes this command did not request: handoff:read, handoff:write\./);
  } finally {
    t.cleanup();
  }
});

test("a workspace binding resumes like a project one", async () => {
  const t = lapsing(resumedGrant);
  try {
    const workspace = { ...t.base, projectId: undefined, workspaceId: WORKSPACE, tools: true };
    const first = await connect(workspace);
    t.lapse();
    const mark = t.calls.length;
    assert.deepEqual(await connect(workspace), first);
    assert.equal(t.since(mark).some((u) => u.endsWith("/cli-auth/start")), false);
    assert.match(t.messages.at(-1)!, new RegExp(`^Resumed binding: workspace ${WORKSPACE};`));
  } finally {
    t.cleanup();
  }
});

test("a refused or unavailable resume runs the full login and consent flow", async () => {
  for (const reply of [
    // Revoked, disconnected, out of reach, or more than 30 days lapsed.
    { code: 403, body: { detail: "Integration access denied" } },
    // The stored CLI sign-in ended.
    { code: 401, body: { detail: "expired" } },
    // A NOUS without the resume route.
    { code: 404, body: { detail: "Not Found" } },
    { code: 503, body: { detail: "down" } },
  ]) {
    const t = lapsing(() => reply);
    try {
      const first = await connect({ ...t.base, tools: true });
      t.lapse();
      const mark = t.calls.length;
      const second = await connect({ ...t.base, tools: true });
      const later = t.since(mark);
      assert.ok(later.some((u) => u.endsWith("/resume")), `resume tried before ${reply.code}`);
      assert.ok(later.some((u) => u.endsWith("/cli-auth/start")), `full flow after ${reply.code}`);
      assert.ok(later.some((u) => u.endsWith("/integrations/grant-requests")));
      assert.notEqual(second.credentialHandle, first.credentialHandle);
      assert.equal(t.messages.some((m) => /^(Reusing|Resumed) binding/.test(m)), false);
    } finally {
      t.cleanup();
    }
  }
});

test("a requested scope missing from the stored binding runs the full flow", async () => {
  const t = setup();
  try {
    const first = await connect({ ...t.base, threadId: CHAT, tools: true });
    const mark = t.calls.length;
    const second = await connect({ ...t.base, threadId: CHAT, tools: true, handoff: true });
    assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), false);
    assert.equal(t.state().credentialHandle, second.credentialHandle);
    const later = t.since(mark);
    // No probe or resume is spent when the scopes already rule reuse out.
    assert.equal(later.some((u) => u.endsWith("/renew") || u.endsWith("/resume")), false);
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

// Cleanup ordering guard: removing the credential before writeLocal("connection")
// makes this test lose the still-current credential on a failed durable write.
test("failed connection persistence keeps the previous binding and credential", async () => {
  const t = setup(() => ({ code: 403, body: { detail: "expired" } }));
  try {
    const first = await connect({ ...t.base, threadId: CHAT });
    const writeLocal = CredentialStore.prototype.writeLocal;
    const write = mock.method(CredentialStore.prototype, "writeLocal", async function (this: CredentialStore, name: string, value: unknown) {
      if (name === "connection") throw new Error("disk write failed");
      return writeLocal.call(this, name, value);
    });
    try {
      await assert.rejects(connect({ ...t.base, threadId: CHAT }), /disk write failed/);
      assert.equal(t.state().credentialHandle, first.credentialHandle);
      assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), true);
    } finally {
      write.mock.restore();
    }
  } finally {
    t.cleanup();
  }
});

test("cleanup failure preserves the new binding and reports retryable local cleanup", async () => {
  const t = setup(() => ({ code: 403, body: { detail: "expired" } }));
  try {
    const first = await connect({ ...t.base, threadId: CHAT });
    const remove = mock.method(CredentialStore.prototype, "removeLocal", async () => {
      throw new Error("cannot unlink credential");
    });
    try {
      const second = await connect({ ...t.base, threadId: CHAT });
      assert.equal(t.state().credentialHandle, second.credentialHandle);
      assert.equal(existsSync(join(t.dir, `${second.credentialHandle}.json`)), true);
      assert.equal(existsSync(join(t.dir, `${first.credentialHandle}.json`)), true);
      assert.match(t.messages.join("\n"), /Could not remove the previous binding's local credential/);
    } finally {
      remove.mock.restore();
    }
  } finally {
    t.cleanup();
  }
});

test("connect with an access token skips the CLI login", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, tools: true, accessToken: "cli-from-rc" });
    assert.equal(
      (await new CredentialStore(t.dir).load(t.state().credentialHandle)).accessToken,
      "cli-from-rc",
    );
    assert.ok(!t.messages.join("\n").includes("cli-from-rc"));
    assert.equal(t.calls.filter((u) => u.includes("/cli-auth/")).length, 0);
    assert.equal(t.calls.filter((u) => u.endsWith("/integrations/devices")).length, 1);
    // The device is registered with the given token, not a fresh login's.
    assert.deepEqual(t.deviceAuth, ["Bearer cli-from-rc"]);
    assert.equal(t.state().projectId, PROJECT);
    assert.deepEqual(t.state().scopes, ["harness:execute", "tools:read"]);
  } finally {
    t.cleanup();
  }
});

const APPROVE_PATH = `/integrations/approve?request_id=${GRANT}`;
const approveLine = (link: string) => `Approve this device and project in your browser: ${link}`;

test("the root-relative approval link prints in full on the website login() saw", async () => {
  const t = setup();
  try {
    // The full flow signs in first; the fake's login page is https://nous.test/l.
    await connect({ ...t.base, tools: true });
    assert.ok(t.messages.includes(approveLine(`https://nous.test${APPROVE_PATH}`)), t.messages.join("\n"));
  } finally {
    t.cleanup();
  }
});

test("without a known website the approval link is the path, with a hint", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, tools: true, accessToken: "cli-from-rc" });
    assert.ok(
      t.messages.includes(approveLine(`${APPROVE_PATH} (open this path on your NOUS website)`)),
      t.messages.join("\n"),
    );
  } finally {
    t.cleanup();
  }
});

test("without a known website, a path that normalises to //host is refused", async () => {
  const t = setup();
  try {
    const fetchFn = (async (input: any, init?: RequestInit) =>
      String(input).endsWith("/grant-requests")
        ? new Response(JSON.stringify({ id: GRANT, approval_url: "/..//evil.example/x" }), { status: 200 })
        : t.base.fetchFn(input, init)) as typeof fetch;
    await assert.rejects(
      connect({ ...t.base, fetchFn, tools: true, accessToken: "cli-from-rc" }),
      { message: "invalid NOUS response" },
    );
    assert.ok(!t.messages.join("\n").includes("evil.example"));
  } finally {
    t.cleanup();
  }
});

test("a website recorded for another API is ignored; an explicit one is used", async () => {
  const t = setup();
  try {
    await new CredentialStore(t.dir).writeLocal("website", {
      apiUrl: "https://elsewhere.test/api/v1",
      origin: "https://elsewhere.test",
    });
    await connect({ ...t.base, tools: true, accessToken: "cli-from-rc" });
    assert.ok(t.messages.includes(approveLine(`${APPROVE_PATH} (open this path on your NOUS website)`)));
    assert.ok(!t.messages.join("\n").includes("elsewhere"));
    await connect({ ...t.base, tools: true, publish: true, accessToken: "cli-from-rc", websiteOrigin: "https://site.test" });
    assert.ok(t.messages.includes(approveLine(`https://site.test${APPROVE_PATH}`)), t.messages.join("\n"));
  } finally {
    t.cleanup();
  }
});

test("login() returns the CLI token after browser approval", async () => {
  const t = setup();
  try {
    const token = await login("https://nous.test/api/v1", t.base);
    assert.equal(token, "cli-secret");
    assert.ok(t.calls.some((u) => u.endsWith("/cli-auth/start")));
    assert.ok(t.messages.some((m) => m.includes("ABCD-1234")));
  } finally {
    t.cleanup();
  }
});

test("an account binding is reused like a project one, and never reuses a project binding or is reused by one", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    await connect({ ...noProject, account: true });
    assert.equal(t.state().account, true);
    assert.deepEqual(t.state().scopes, ["harness:execute", "tools:read", "library:read"]);
    let mark = t.calls.length;
    await connect({ ...noProject, account: true });
    assert.deepEqual(t.since(mark), [`https://nous.test/api/v1/integrations/grants/${GRANT}/renew`]);
    assert.match(t.messages.at(-1)!, /^Reusing binding: your NOUS account; no new login or consent needed\.$/);
    mark = t.calls.length;
    await connect({ ...t.base, tools: true });
    assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")), "account never stands in for a project");
    mark = t.calls.length;
    await connect({ ...noProject, account: true });
    assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")), "a project never stands in for the account");
  } finally {
    t.cleanup();
  }
});

test("a lapsed account grant resumes from its consent with no login, consent or new device", async () => {
  const t = lapsing(resumedGrant);
  try {
    const account = { ...t.base, projectId: undefined, account: true };
    const first = await connect(account);
    t.lapse();
    const mark = t.calls.length;
    assert.deepEqual(await connect(account), first);
    assert.equal(t.since(mark).some((u) => u.endsWith("/cli-auth/start") || u.endsWith("/grant-requests")), false);
    assert.match(t.messages.at(-1)!, /^Resumed binding: your NOUS account; your earlier approval still holds/);
  } finally {
    t.cleanup();
  }
});

test("a stored account connection loads; one that also names a project, or says account: false, is not connected", async () => {
  for (const [name, binding, connected] of [
    ["account", { account: true }, true],
    ["account and project", { account: true, projectId: PROJECT }, false],
    ["account false", { account: false }, false],
  ] as const) {
    const t = setup();
    try {
      await connect({ ...t.base, tools: true });
      const { projectId: _project, ...rest } = t.state();
      await new CredentialStore(t.dir).writeLocal("connection", { ...rest, ...binding });
      const lines: string[] = [];
      await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
      assert.equal(/not connected/i.test(lines.join("\n")), !connected, name);
    } finally {
      t.cleanup();
    }
  }
});

test("status names an account binding and shows neither a project nor a chat", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, projectId: undefined, account: true });
    const lines: string[] = [];
    await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
    const out = lines.join("\n");
    assert.match(out, /^Account: your whole NOUS account \(every project you own or belong to in your organization\)$/m);
    assert.doesNotMatch(out, /Project:|Chat:|Workspace:/);
    assert.match(out, /Scopes: harness:execute tools:read library:read/);
  } finally {
    t.cleanup();
  }
});

test("handoff commands refuse an account connection and say how to get a chat one", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, projectId: undefined, account: true });
    const mark = t.calls.length;
    const refusal = /whole NOUS account, which has no chat handoff.*connect --project UUID --chat UUID --tools --handoff/;
    const options = { stateDir: t.dir, fetchFn: t.base.fetchFn };
    await assert.rejects(handoffShow(options), refusal);
    await assert.rejects(handoffFlush(options), refusal);
    assert.equal(t.calls.length, mark);
  } finally {
    t.cleanup();
  }
});
