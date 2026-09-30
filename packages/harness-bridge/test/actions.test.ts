import assert from "node:assert/strict";
import { test } from "node:test";
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage } from "node:http";
import { once } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ActionHttpClient, type ActionStatus } from "../src/actions/client.ts";
import { actionStatusTool, describe, requestActionTool } from "../src/actions/mcp.ts";
import { connect, sessionOptionsFor, type LocalState } from "../src/cli.ts";
import { CapabilityClient, ToolRequestRejected } from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";

const credentials = { accessToken: "cli-jwt", grantToken: "nous_ig_" + "a".repeat(43) };
const INVOCATION = "44444444-4444-4444-8444-444444444444";
const APPROVAL = `https://app.example/integrations/actions/${INVOCATION}`;

function status(state: ActionStatus["state"], extra: Partial<ActionStatus> = {}): ActionStatus {
  return {
    invocation_id: INVOCATION,
    state,
    tool_name: "create_project_note",
    result: null,
    approval_url: state === "awaiting_approval" ? APPROVAL : null,
    ...extra,
  } as ActionStatus;
}

async function backend(reply: () => { code: number; body: unknown }) {
  const requests: { method: string; path: string; headers: IncomingMessage["headers"]; body: string }[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c) => chunks.push(c));
    request.on("end", () => {
      requests.push({ method: request.method ?? "", path: request.url ?? "", headers: request.headers, body: Buffer.concat(chunks).toString() });
      const { code, body } = reply();
      response.writeHead(code, { "Content-Type": "application/json" });
      response.end(JSON.stringify(body));
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

test("only a succeeded action is described as created", () => {
  const pending = describe(status("awaiting_approval"));
  assert.match(pending, /^Not created yet/);
  assert.ok(pending.includes(APPROVAL));
  assert.match(pending, /do not request it again/);
  for (const state of ["approved", "executing"] as const) assert.doesNotMatch(describe(status(state)), /^Created/);
  assert.match(describe(status("failed", { result: { content: [{ error: "denied by user" }], is_error: true, source_refs: [] } })), /^Not created: .*denied by user/);
  assert.match(describe(status("outcome_unknown")), /Do not retry/);
  assert.match(
    describe(status("succeeded", { result: { content: [{ note_id: "n1" }], is_error: false, source_refs: [] } })),
    /^Created\. .*n1/,
  );
});

test("request_action sends only the note fields with the grant headers and never identity", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    const outcome = await tool.call({
      action: "create_project_note",
      invocation_id: INVOCATION,
      title: "Findings",
      content: "# Findings",
      tags: ["a"],
      project_id: randomUUID(),
      user_id: randomUUID(),
    });
    assert.equal(outcome.isError, undefined);
    assert.match(outcome.text, /^Not created yet/);
    assert.equal(server.requests.length, 1);
    const [sent] = server.requests;
    assert.equal(sent?.method, "POST");
    assert.equal(sent?.path, "/api/v1/integrations/actions");
    assert.equal(sent?.headers["x-nous-integration-grant"], credentials.grantToken);
    assert.deepEqual(JSON.parse(sent?.body ?? "{}"), {
      tool_name: "create_project_note",
      invocation_id: INVOCATION,
      arguments: { title: "Findings", content: "# Findings", tags: ["a"] },
    });
  } finally {
    await server.close();
  }
});

test("invalid arguments are refused locally before any request", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const client = new ActionHttpClient(server.origin, credentials);
    const request = requestActionTool(client);
    for (const args of [
      { action: "delete_project", invocation_id: INVOCATION, title: "t", content: "c" },
      { action: "create_project_note", invocation_id: "not-a-uuid", title: "t", content: "c" },
      { action: "create_project_note", invocation_id: INVOCATION, title: " ", content: "c" },
      { action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "" },
      { action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c", tags: "a" },
    ]) {
      const outcome = await request.call(args);
      assert.equal(outcome.isError, true, JSON.stringify(args));
    }
    const statusCheck = await actionStatusTool(client).call({ invocation_id: "x" });
    assert.equal(statusCheck.isError, true);
    assert.equal(server.requests.length, 0);
  } finally {
    await server.close();
  }
});

test("gateway rejections surface as stable errors and a lost reply says how to retry safely", async () => {
  let code = 403;
  const server = await backend(() => ({ code, body: { detail: "Action request conflicts with an existing one" } }));
  const args = { action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c" };
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    await assert.rejects(tool.call(args), (error: unknown) => error instanceof ToolRequestRejected && /tools:write/.test((error as Error).message));
    code = 409;
    await assert.rejects(tool.call(args), /conflicts with an existing one/);
  } finally {
    await server.close();
  }
  // Connection refused after the server is gone: the outcome is unknown.
  const offline = requestActionTool(new ActionHttpClient(server.origin, credentials));
  const lost = await offline.call(args);
  assert.equal(lost.isError, true);
  assert.match(lost.text, new RegExp(`same invocation_id ${INVOCATION}`));
});

test("get_action_status reads the stored state and flags failures", async () => {
  let reply = status("succeeded", { result: { content: [{ note_id: "n1" }], is_error: false, source_refs: [] } });
  const server = await backend(() => ({ code: 200, body: reply }));
  try {
    const tool = actionStatusTool(new ActionHttpClient(server.origin, credentials));
    const done = await tool.call({ invocation_id: INVOCATION });
    assert.equal(done.isError, undefined);
    assert.match(done.text, /^Created/);
    assert.equal(server.requests[0]?.method, "GET");
    assert.equal(server.requests[0]?.path, `/api/v1/integrations/actions/${INVOCATION}`);
    reply = status("outcome_unknown");
    assert.equal((await tool.call({ invocation_id: INVOCATION })).isError, true);
    reply = { invocation_id: INVOCATION, state: "made_up" } as unknown as ActionStatus;
    await assert.rejects(tool.call({ invocation_id: INVOCATION }), /invalid NOUS action status/);
  } finally {
    await server.close();
  }
});

test("a status 404 after reconnecting says not to request the action again", async () => {
  const server = await backend(() => ({ code: 404, body: { detail: "Action not found" } }));
  try {
    const outcome = await actionStatusTool(new ActionHttpClient(server.origin, credentials)).call({ invocation_id: INVOCATION });
    assert.equal(outcome.isError, true);
    assert.match(outcome.text, /earlier connection\. Do not request it again/);
  } finally {
    await server.close();
  }
});

test("the MCP server offers request and status tools but no way to approve", async () => {
  const server = await backend(() => ({ code: 200, body: [] }));
  try {
    const actions = new ActionHttpClient(server.origin, credentials);
    const mcp = createNousMcpServer(new CapabilityClient(server.origin, credentials), [
      requestActionTool(actions),
      actionStatusTool(actions),
    ]);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await mcp.connect(serverSide);
    const client = new Client({ name: "test", version: "0" });
    await client.connect(clientSide);
    const names = (await client.listTools()).tools.map((t) => t.name).sort();
    assert.deepEqual(names, ["get_action_status", "request_action"]);
    assert.equal(names.some((n) => /approve|decide|decision/.test(n)), false);
    await client.close();
  } finally {
    await server.close();
  }
});

test("managed sessions expose actions only when tools:write was granted, and --write needs --tools", async () => {
  const binding = { id: randomUUID(), root: "/tmp", label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    projectId: binding.projectId,
    credentialHandle: randomUUID(),
    scopes: ["harness:execute", "tools:read"],
    workspaces: [binding],
  };
  const args = (s: LocalState) => sessionOptionsFor("/tmp/state", s, binding.id).mcpConfig?.nous?.args ?? [];
  assert.equal(args(state).includes("--actions"), false);
  state.scopes = ["harness:execute", "tools:read", "tools:write"];
  assert.equal(args(state).includes("--actions"), true);
  assert.equal(args(state).includes(state.credentialHandle), true);
  await assert.rejects(
    connect({ stateDir: "/tmp/unused", apiUrl: "https://nous.example/api/v1", projectId: randomUUID(), label: "d", write: true }),
    /--write requires --tools/,
  );
});
