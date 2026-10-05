import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { connect } from "../src/cli.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const CHAT = "99999999-9999-4999-8999-999999999999";

function fakeNous(calls: { url: string; body: any }[]): typeof fetch {
  return (async (input: any, init: any) => {
    const url = String(input);
    calls.push({ url, body: init.body ? JSON.parse(init.body) : undefined });
    let data: object;
    if (url.endsWith("/cli-auth/start"))
      data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l" };
    else if (url.includes("/cli-auth/status/")) data = { status: "approved", token: "cli" };
    else if (url.endsWith("/integrations/devices"))
      data = { id: "22222222-2222-4222-8222-222222222222" };
    else if (url.endsWith("/grant-requests"))
      data = {
        id: "33333333-3333-4333-8333-333333333333",
        approval_url: "https://nous.test/a",
        thread_label: "Literature\x1b review\x07",
      };
    else if (url.endsWith("/exchange")) data = { token: "grant" };
    else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
}

test("connect --chat binds the consent request and local state to one chat", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-connect-chat-"));
  const calls: { url: string; body: any }[] = [];
  const messages: string[] = [];
  const base = {
    fetchFn: fakeNous(calls),
    announce: (m: string) => messages.push(m),
    apiUrl: "https://nous.test/api/v1",
    projectId: PROJECT,
    label: "Laptop",
    stateDir: dir,
  };
  try {
    await connect({ ...base, threadId: CHAT });
    const bound = calls.filter((c) => c.url.endsWith("/grant-requests")).at(-1)!;
    assert.equal(bound.body.thread_id, CHAT);
    const state = () => JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.equal(state().threadId, CHAT);
    // Server-supplied labels cannot inject terminal control sequences.
    assert.ok(messages.at(-1)!.includes(`chat Literature review (${CHAT})`));

    // Reconnecting without --chat leaves the request unchanged and unbinds.
    await connect(base);
    const plain = calls.filter((c) => c.url.endsWith("/grant-requests")).at(-1)!;
    assert.deepEqual(Object.keys(plain.body).sort(), ["device_id", "project_id", "scopes"]);
    assert.equal(state().threadId, undefined);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("connect --chat rejects a missing project and a non-UUID chat before any request", async () => {
  const calls: { url: string; body: any }[] = [];
  const base = {
    fetchFn: fakeNous(calls),
    announce: () => {},
    apiUrl: "https://nous.test/api/v1",
    label: "Laptop",
    stateDir: join(tmpdir(), "nous-connect-chat-unused"),
  };
  await assert.rejects(connect({ ...base, projectId: "", threadId: CHAT }), /--chat requires --project/);
  await assert.rejects(
    connect({ ...base, projectId: "not-a-uuid", threadId: CHAT }),
    /project UUID and device label required/,
  );
  await assert.rejects(
    connect({ ...base, projectId: PROJECT, threadId: "not-a-uuid" }),
    /--chat must be a chat UUID/,
  );
  assert.equal(calls.length, 0);
});
