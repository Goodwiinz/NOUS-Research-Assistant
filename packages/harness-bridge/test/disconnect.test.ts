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
const RESUMED = "44444444-4444-4444-8444-444444444444";

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

/** A grant last renewed 20 minutes ago: expired, so its renewal is refused. */
async function lapsed(resume: () => Response) {
  const c = await connected({ grantId: GRANT, renewedAt: Date.now() - 20 * 60_000 });
  const reply = () => {
    const url = c.calls.at(-1)!.url;
    if (url.endsWith("/renew")) return new Response("{}", { status: 403 });
    if (url.endsWith("/resume")) return resume();
    return new Response(null, { status: 204 });
  };
  return { ...c, run: () => c.run(reply) };
}

test("a lapsed grant is resumed from its consent so disconnect still revokes it", async () => {
  const c = await lapsed(
    () => new Response(JSON.stringify({ token: "grant-resumed", grant_id: RESUMED }), { status: 200 }),
  );
  try {
    assert.deepEqual(await c.run(), { revoked: true });
    assert.deepEqual(
      c.calls.map((call) => `${call.method} ${call.url}`),
      [
        `POST ${API}/integrations/grants/${GRANT}/renew`,
        `POST ${API}/integrations/grants/${GRANT}/resume`,
        `DELETE ${API}/integrations/grants/${RESUMED}`,
      ],
    );
    assert.equal(c.calls[1]?.headers.get("x-nous-integration-grant"), "grant-live");
    assert.equal(c.calls[2]?.headers.get("x-nous-integration-grant"), "grant-resumed");
    assert.equal(await c.exists(c.credentialHandle), false);
    assert.equal(await c.exists("connection"), false);
    assert.match(c.announced.join("\n"), /Disconnected/);
  } finally {
    c.cleanup();
  }
});

test("a lapsed grant NOUS will not resume is forgotten locally with a pointer to the browser", async () => {
  for (const status of [401, 403, 404]) {
    const c = await lapsed(() => new Response("{}", { status }));
    try {
      assert.deepEqual(await c.run(), { revoked: false });
      assert.ok(c.calls.some((call) => call.url.endsWith("/resume")), `resume tried before ${status}`);
      assert.equal(c.calls.some((call) => call.method === "DELETE"), false);
      assert.equal(await c.exists(c.credentialHandle), false);
      assert.equal(await c.exists("connection"), false);
      assert.match(c.announced.join("\n"), /\/integrations\/devices/);
    } finally {
      c.cleanup();
    }
  }
});

test("a lapsed grant whose resume gets no answer keeps everything so disconnect can be retried", async () => {
  for (const resume of [
    () => new Response("{}", { status: 503 }),
    () => {
      throw new TypeError("fetch failed");
    },
  ]) {
    const c = await lapsed(resume);
    try {
      await assert.rejects(c.run(), /nothing was removed/);
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
