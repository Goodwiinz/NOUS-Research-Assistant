import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { disconnect } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";

const API = "https://nous.test/api/v1";
const GRANT = "33333333-3333-4333-8333-333333333333";

type Call = { url: string; method: string; headers: Headers };

async function connected(extra: Record<string, unknown> = { grantId: GRANT, renewedAt: Date.now() }) {
  const dir = mkdtempSync(join(tmpdir(), "nous-disconnect-"));
  const stateDir = join(dir, "state");
  const store = new CredentialStore(stateDir);
  const credentialHandle = await store.save({ accessToken: "cli-jwt", grantToken: "grant-live", ...extra });
  await store.writeLocal("connection", {
    apiUrl: API,
    deviceId: randomUUID(),
    projectId: randomUUID(),
    credentialHandle,
    scopes: ["harness:execute"],
    workspaces: [],
  });
  const calls: Call[] = [];
  const announced: string[] = [];
  const run = (reply: () => Response | Promise<Response>) =>
    disconnect({
      stateDir,
      announce: (m) => announced.push(m),
      fetchFn: (async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), method: init?.method ?? "GET", headers: new Headers(init?.headers) });
        return reply();
      }) as typeof fetch,
    });
  const exists = async (name: string) =>
    store.readLocal(name).then(
      () => true,
      () => false,
    );
  return { store, credentialHandle, calls, announced, run, exists, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test("disconnect revokes the grant in NOUS, then forgets local credentials", async () => {
  const c = await connected();
  try {
    const result = await c.run(() => new Response(null, { status: 204 }));
    assert.deepEqual(result, { revoked: true });
    assert.equal(c.calls.length, 1);
    assert.equal(c.calls[0]?.method, "DELETE");
    assert.equal(c.calls[0]?.url, `${API}/integrations/grants/${GRANT}`);
    assert.equal(c.calls[0]?.headers.get("x-nous-integration-grant"), "grant-live");
    assert.equal(c.calls[0]?.headers.get("authorization"), "Bearer cli-jwt");
    assert.equal(await c.exists(c.credentialHandle), false);
    assert.equal(await c.exists("connection"), false);
    assert.match(c.announced.join("\n"), /Disconnected/);
  } finally {
    c.cleanup();
  }
});

test("a grant NOUS already refuses is forgotten locally with a pointer to the browser", async () => {
  for (const status of [401, 403, 404]) {
    const c = await connected();
    try {
      assert.deepEqual(await c.run(() => new Response("{}", { status })), { revoked: false });
      assert.equal(await c.exists(c.credentialHandle), false);
      assert.match(c.announced.join("\n"), /\/integrations\/devices/);
    } finally {
      c.cleanup();
    }
  }
});

test("a network or server failure keeps everything so the command can be retried", async () => {
  for (const reply of [
    () => new Response("{}", { status: 503 }),
    () => {
      throw new TypeError("fetch failed");
    },
  ]) {
    const c = await connected();
    try {
      await assert.rejects(c.run(reply), /nothing was removed/);
      assert.equal(await c.exists(c.credentialHandle), true);
      assert.equal(await c.exists("connection"), true);
    } finally {
      c.cleanup();
    }
  }
});

test("a connection from before grant renewal cannot revoke itself and says where to", async () => {
  const c = await connected({});
  try {
    assert.deepEqual(await c.run(() => new Response(null, { status: 204 })), { revoked: false });
    assert.equal(c.calls.length, 0);
    assert.equal(await c.exists(c.credentialHandle), false);
    assert.match(c.announced.join("\n"), /\/integrations\/devices/);
  } finally {
    c.cleanup();
  }
});

test("an unconnected device is told so", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-disconnect-"));
  try {
    await assert.rejects(disconnect({ stateDir: join(dir, "state") }), /not connected/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
