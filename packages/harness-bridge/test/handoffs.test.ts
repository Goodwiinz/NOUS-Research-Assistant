import assert from "node:assert/strict";
import { after, test } from "node:test";
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage } from "node:http";
import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { connect, sessionOptionsFor, type LocalState } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";
import { GrantKeeper } from "../src/grants.ts";
import { HandoffHttpClient, type Handoff } from "../src/handoffs/client.ts";
import { getHandoffTool, saveHandoffTool } from "../src/handoffs/mcp.ts";
import { HandoffQueue } from "../src/handoffs/queue.ts";
import { CapabilityClient, ToolRequestRejected } from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";

const credentials = { accessToken: "cli-jwt", grantToken: "nous_ig_" + "a".repeat(43) };
const THREAD = "99999999-9999-4999-8999-999999999999";
const VERSION = "55555555-5555-4555-8555-555555555555";

const queueDirs: string[] = [];
after(() => queueDirs.forEach((dir) => rmSync(dir, { recursive: true, force: true })));
/** save_nous_handoff over a throwaway local queue bound to THREAD. */
function saveTool(client: HandoffHttpClient) {
  const dir = mkdtempSync(join(tmpdir(), "nous-handoff-queue-"));
  queueDirs.push(dir);
  return saveHandoffTool(new HandoffQueue(new CredentialStore(dir), client), async () => ({
    threadId: THREAD,
    projectId: randomUUID(),
  }));
}

function handoff(version: number): Handoff {
  return {
    id: randomUUID(),
    thread_id: THREAD,
    project_id: randomUUID(),
    version,
    handoff_id: randomUUID(),
    goal: "Finish the review",
    decisions: ["PRISMA"],
    remaining: ["Screen"],
    results: [{ artifact_version_id: VERSION, summary: "log" }],
    harness_name: "codex",
    harness_session_id: null,
    created_at: "2026-10-05T00:00:00Z",
  };
}

type Seen = { method: string; path: string; headers: IncomingMessage["headers"]; rawHeaders: string[]; body: string };

async function backend(reply: (request: Seen) => { code: number; body: unknown }) {
  const requests: Seen[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c) => chunks.push(c));
    request.on("end", () => {
      const seen = {
        method: request.method ?? "",
        path: request.url ?? "",
        headers: request.headers,
        rawHeaders: request.rawHeaders,
        body: Buffer.concat(chunks).toString(),
      };
      requests.push(seen);
      const { code, body } = reply(seen);
      response.writeHead(code, { "Content-Type": "application/json" });
      response.end(typeof body === "string" ? body : JSON.stringify(body));
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  return {
    requests,
    origin: `http://127.0.0.1:${address.port}/api/v1`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

test("handoff tools are advertised only when the grant carries the handoff scopes", async () => {
  const binding = { id: randomUUID(), root: "/tmp", label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    projectId: binding.projectId,
    threadId: THREAD,
    credentialHandle: "handle-1",
    scopes: ["harness:execute", "tools:read"],
    workspaces: [binding],
  };
  const args = (s: LocalState) => sessionOptionsFor("/tmp/state", s, binding.id).mcpConfig?.nous?.args ?? [];
  assert.equal(args(state).includes("--handoff"), false);
  state.scopes = ["harness:execute", "tools:read", "handoff:read"];
  assert.equal(args(state).includes("--handoff"), false);
  state.scopes = ["harness:execute", "tools:read", "handoff:read", "handoff:write"];
  assert.equal(args(state).includes("--handoff"), true);
});

test("the MCP server lists both handoff tools when they are registered", async () => {
  const server = await backend(() => ({ code: 200, body: [] }));
  try {
    const handoffs = new HandoffHttpClient(server.origin, credentials);
    const mcp = createNousMcpServer(new CapabilityClient(server.origin, credentials), [
      getHandoffTool(handoffs),
      saveTool(handoffs),
    ]);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await mcp.connect(serverSide);
    const client = new Client({ name: "test", version: "0" });
    await client.connect(clientSide);
    const names = (await client.listTools()).tools.map((t) => t.name).sort();
    assert.deepEqual(names, ["get_nous_handoff", "save_nous_handoff"]);
    await client.close();
  } finally {
    await server.close();
  }
});

test("save sends the keeper's bound grant header exactly once and only handoff fields", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-handoff-"));
  const server = await backend(() => ({ code: 200, body: handoff(1) }));
  try {
    const store = new CredentialStore(dir);
    const handle = await store.save(credentials);
    const keeper = new GrantKeeper(store, handle, server.origin);
    // Same wiring as stdio: static headers plus the keeper's fetch.
    const tool = saveTool(new HandoffHttpClient(server.origin, credentials, keeper.fetch));
    const outcome = await tool.call({
      expected_parent_version: null,
      goal: "Finish the review",
      results: [{ artifact_version_id: VERSION, summary: "log" }],
      thread_id: randomUUID(),
      project_id: randomUUID(),
    });
    assert.equal(outcome.isError, undefined);
    assert.match(outcome.text, /^Saved handoff version 1/);
    const [request] = server.requests;
    assert.equal(request.method, "POST");
    assert.equal(request.path, "/api/v1/integrations/handoffs");
    const grantHeaders = request.rawHeaders.filter((_, i, all) => i % 2 === 0 && all[i].toLowerCase() === "x-nous-integration-grant");
    assert.equal(grantHeaders.length, 1);
    assert.equal(request.headers["x-nous-integration-grant"], credentials.grantToken);
    const body = JSON.parse(request.body);
    assert.deepEqual(Object.keys(body).sort(), [
      "decisions", "expected_parent_version", "goal", "handoff_id", "harness_name", "remaining", "results",
    ]);
    assert.match(body.handoff_id, /^[0-9a-f-]{36}$/);
    assert.equal(body.harness_name, "codex");
  } finally {
    await server.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a 409 is returned as an isError result carrying the latest version, not thrown", async () => {
  const latest = handoff(4);
  const server = await backend(() => ({ code: 409, body: { detail: "Handoff is stale", latest } }));
  try {
    const tool = saveTool(new HandoffHttpClient(server.origin, credentials));
    const outcome = await tool.call({ expected_parent_version: 2, goal: "Stale" });
    assert.equal(outcome.isError, true);
    assert.match(outcome.text, /latest handoff is version 4/);
    assert.match(outcome.text, /expected_parent_version 4/);
    assert.equal((outcome.structured as Handoff).version, 4);
  } finally {
    await server.close();
  }
});

test("a 409 envelope without a latest version surfaces its detail", async () => {
  const server = await backend(() => ({
    code: 409,
    body: { detail: "Handoff version conflict", latest: null },
  }));
  try {
    const tool = saveTool(new HandoffHttpClient(server.origin, credentials));
    const outcome = await tool.call({ expected_parent_version: 1, goal: "First" });
    assert.equal(outcome.isError, true);
    assert.match(outcome.text, /^Not saved: Handoff version conflict/);
  } finally {
    await server.close();
  }
});

test("a 409 with an unparsable body is a conflict, not an unknown outcome", async () => {
  for (const body of ["not json at all", { latest: { version: "x" } }, { something: 1 }]) {
    const server = await backend(() => ({ code: 409, body }));
    try {
      const client = new HandoffHttpClient(server.origin, credentials);
      const payload = {
        handoff_id: randomUUID(), expected_parent_version: null, goal: "g", harness_name: "codex",
      };
      assert.deepEqual(await client.save(payload), { conflict: null, detail: "NOUS reported a conflict" });
      const outcome = await saveTool(client).call({ expected_parent_version: null, goal: "g" });
      assert.equal(outcome.isError, true);
      assert.doesNotMatch(outcome.text, /unknown/);
    } finally {
      await server.close();
    }
  }
});

test("get_nous_handoff reads the latest, reports none on 404, and rejects on 403", async () => {
  let code = 200;
  const server = await backend(() => ({
    code,
    body: code === 200 ? handoff(2) : { detail: "x" },
  }));
  try {
    const tool = getHandoffTool(new HandoffHttpClient(server.origin, credentials));
    const read = await tool.call({});
    assert.equal((read.structured as Handoff).version, 2);
    assert.equal(server.requests[0].path, "/api/v1/integrations/handoffs/latest");
    code = 404;
    assert.match((await tool.call({})).text, /No handoff exists/);
    code = 403;
    await assert.rejects(tool.call({}), (error: unknown) => error instanceof ToolRequestRejected && /handoff:read/.test(error.message));
  } finally {
    await server.close();
  }
});

test("save_nous_handoff validates arguments locally before any request", async () => {
  const server = await backend(() => ({ code: 200, body: handoff(1) }));
  try {
    const tool = saveTool(new HandoffHttpClient(server.origin, credentials));
    for (const args of [
      { goal: "g" },
      { expected_parent_version: 0, goal: "g" },
      { expected_parent_version: null, goal: "" },
      { expected_parent_version: null, goal: "g", decisions: [1] },
      { expected_parent_version: null, goal: "g", results: [{ artifact_version_id: "nope", summary: "s" }] },
      { expected_parent_version: null, goal: "g", handoff_id: "nope" },
    ])
      assert.equal((await tool.call(args)).isError, true);
    assert.equal(server.requests.length, 0);
  } finally {
    await server.close();
  }
});

test("connect --handoff requires --tools and --chat, and requests both handoff scopes", async () => {
  const base = { stateDir: "/tmp/unused", apiUrl: "https://nous.example/api/v1", projectId: randomUUID(), label: "d" };
  await assert.rejects(connect({ ...base, threadId: THREAD, handoff: true }), /--handoff requires --tools/);
  await assert.rejects(connect({ ...base, tools: true, handoff: true }), /--handoff requires --chat/);
  const dir = mkdtempSync(join(tmpdir(), "nous-handoff-connect-"));
  const scopes: string[][] = [];
  const fetchFn = (async (input: any, init: any) => {
    const url = String(input);
    const body = init.body ? JSON.parse(init.body) : undefined;
    let data: object;
    if (url.endsWith("/cli-auth/start")) data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l" };
    else if (url.includes("/cli-auth/status/")) data = { status: "approved", token: "cli" };
    else if (url.endsWith("/integrations/devices")) data = { id: "22222222-2222-4222-8222-222222222222" };
    else if (url.endsWith("/grant-requests")) {
      scopes.push(body.scopes);
      data = { id: "33333333-3333-4333-8333-333333333333", approval_url: "https://nous.test/a", thread_label: "Chat" };
    } else if (url.endsWith("/exchange")) data = { token: "grant" };
    else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
  try {
    await connect({ ...base, stateDir: dir, threadId: THREAD, tools: true, handoff: true, fetchFn, announce: () => {} });
    assert.deepEqual(scopes.at(-1)!.filter((s) => s.startsWith("handoff:")).sort(), ["handoff:read", "handoff:write"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
