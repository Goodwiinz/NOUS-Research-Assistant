import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CredentialStore, type IntegrationCredentials } from "../src/credentials.ts";
import { GRANT_RENEW_AFTER_MS, GrantExpired, GrantKeeper } from "../src/grants.ts";

const API = "https://nous.test/api/v1";
const OLD_GRANT = "11111111-1111-4111-8111-111111111111";
const NEW_GRANT = "22222222-2222-4222-8222-222222222222";
const T0 = 1_000_000_000_000;

type Call = { url: string; method: string; headers: Headers };

async function setup(credentials: Partial<IntegrationCredentials> = {}) {
  const dir = mkdtempSync(join(tmpdir(), "nous-grants-"));
  const store = new CredentialStore(join(dir, "vault"));
  const handle = await store.save({
    accessToken: "cli-jwt",
    grantToken: "grant-old",
    grantId: OLD_GRANT,
    renewedAt: T0,
    ...credentials,
  });
  const calls: Call[] = [];
  let reply: () => Response | Promise<Response> = () =>
    new Response(JSON.stringify({ token: "grant-new", grant_id: NEW_GRANT }), { status: 200 });
  const fetchFn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), method: init?.method ?? "GET", headers: new Headers(init?.headers) });
    return await reply();
  }) as typeof fetch;
  let clock = T0;
  const keeper = (now = () => clock) =>
    new GrantKeeper(store, handle, API, fetchFn, now, async () => {});
  return {
    store,
    handle,
    calls,
    keeper,
    setReply: (next: () => Response | Promise<Response>) => {
      reply = next;
    },
    advance: (ms: number) => {
      clock += ms;
    },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

test("a fresh grant is used as stored, without a renewal request", async () => {
  const s = await setup();
  try {
    s.advance(GRANT_RENEW_AFTER_MS - 1);
    const credentials = await s.keeper().current();
    assert.equal(credentials.grantToken, "grant-old");
    assert.equal(s.calls.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a due grant is renewed with the old credentials and written back for every process", async () => {
  const s = await setup();
  try {
    s.advance(GRANT_RENEW_AFTER_MS);
    const renewed = await s.keeper().current();
    assert.equal(renewed.grantToken, "grant-new");
    assert.equal(renewed.grantId, NEW_GRANT);
    assert.equal(s.calls.length, 1);
    assert.equal(s.calls[0]?.url, `${API}/integrations/grants/${OLD_GRANT}/renew`);
    assert.equal(s.calls[0]?.method, "POST");
    assert.equal(s.calls[0]?.headers.get("x-nous-integration-grant"), "grant-old");
    assert.equal(s.calls[0]?.headers.get("authorization"), "Bearer cli-jwt");
    // A second process sharing the handle reads the renewed grant, no request.
    const other = await s.keeper().current();
    assert.equal(other.grantToken, "grant-new");
    assert.equal(s.calls.length, 1);
    assert.equal((await s.store.load(s.handle)).grantToken, "grant-new");
  } finally {
    s.cleanup();
  }
});

test("concurrent callers in one process share a single renewal", async () => {
  const s = await setup();
  try {
    s.advance(GRANT_RENEW_AFTER_MS);
    const keeper = s.keeper();
    const results = await Promise.all([keeper.current(), keeper.current(), keeper.current()]);
    assert.deepEqual(results.map((c) => c.grantToken), ["grant-new", "grant-new", "grant-new"]);
    assert.equal(s.calls.length, 1);
  } finally {
    s.cleanup();
  }
});

test("losing a renewal race to another process picks up its grant", async () => {
  const s = await setup();
  try {
    s.advance(GRANT_RENEW_AFTER_MS);
    s.setReply(async () => {
      // The winner's write has landed by the time we hear our token was revoked.
      await s.store.update(s.handle, {
        accessToken: "cli-jwt",
        grantToken: "grant-winner",
        grantId: NEW_GRANT,
        renewedAt: T0 + GRANT_RENEW_AFTER_MS,
      });
      return new Response(JSON.stringify({ detail: "no longer available" }), { status: 409 });
    });
    const credentials = await s.keeper().current();
    assert.equal(credentials.grantToken, "grant-winner");
  } finally {
    s.cleanup();
  }
});

test("a grant that can no longer be renewed says to reconnect", async () => {
  for (const status of [403, 409]) {
    const s = await setup();
    try {
      s.advance(GRANT_RENEW_AFTER_MS);
      s.setReply(() => new Response("{}", { status }));
      await assert.rejects(s.keeper().current(), (error: unknown) => error instanceof GrantExpired && /reconnect/.test((error as Error).message));
      assert.equal((await s.store.load(s.handle)).grantToken, "grant-old");
    } finally {
      s.cleanup();
    }
  }
});

test("a malformed renewal reply is refused and the stored grant kept", async () => {
  const s = await setup();
  try {
    s.advance(GRANT_RENEW_AFTER_MS);
    s.setReply(() => new Response(JSON.stringify({ token: "", grant_id: "x" }), { status: 200 }));
    await assert.rejects(s.keeper().current(), /invalid NOUS grant renewal/);
    assert.equal((await s.store.load(s.handle)).grantToken, "grant-old");
  } finally {
    s.cleanup();
  }
});

test("connections from before renewal keep working unchanged and are never renewed", async () => {
  const s = await setup({ grantId: undefined, renewedAt: undefined });
  try {
    s.advance(GRANT_RENEW_AFTER_MS * 10);
    const credentials = await s.keeper().current();
    assert.equal(credentials.grantToken, "grant-old");
    assert.equal(s.calls.length, 0);
  } finally {
    s.cleanup();
  }
});

test("the keeper's fetch sends the current grant and keeps the caller's request", async () => {
  const s = await setup();
  try {
    const keeper = s.keeper();
    s.advance(GRANT_RENEW_AFTER_MS);
    await keeper.fetch(`${API}/integrations/tools`, {
      method: "POST",
      headers: { "X-NOUS-Integration-Grant": "stale-captured-token", "Content-Type": "application/json" },
      body: "{}",
    });
    const sent = s.calls.at(-1)!;
    assert.equal(sent.url, `${API}/integrations/tools`);
    assert.equal(sent.method, "POST");
    assert.equal(sent.headers.get("x-nous-integration-grant"), "grant-new");
    assert.equal(sent.headers.get("content-type"), "application/json");
  } finally {
    s.cleanup();
  }
});
