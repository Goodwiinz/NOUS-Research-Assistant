import assert from "node:assert/strict";
import { test } from "node:test";
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage } from "node:http";
import { once } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { connect, sessionOptionsFor, type LocalState } from "../src/cli.ts";
import { ContextHttpClient, readSelectedContextTool } from "../src/context/mcp.ts";
import { CapabilityClient, ToolRequestRejected } from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";

const credentials = { accessToken: "cli-jwt", grantToken: "nous_ig_" + "b".repeat(43) };

async function backend(reply: () => { code: number; body: unknown }) {
  const requests: { method: string; path: string; headers: IncomingMessage["headers"] }[] = [];
  const server = createServer((request, response) => {
    requests.push({ method: request.method ?? "", path: request.url ?? "", headers: request.headers });
    const { code, body } = reply();
    response.writeHead(code, { "Content-Type": "application/json" });
    response.end(JSON.stringify(body));
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

test("read_selected_context returns exactly what NOUS shared, with provenance", async () => {
  const shared = {
    content: [{ memories: [{ memory_id: "m1", content: "Cite in APA" }] }],
    is_error: false,
    source_refs: [{ memory_id: "m1" }],
  };
  const server = await backend(() => ({ code: 200, body: shared }));
  try {
    const outcome = await readSelectedContextTool(new ContextHttpClient(server.origin, credentials)).call({
      memory_ids: ["m2"],
      project_id: randomUUID(),
    });
    assert.equal(outcome.isError, undefined);
    assert.deepEqual(outcome.structured, { content: shared.content, source_refs: shared.source_refs });
    // Model arguments never reach NOUS: the request has no body or query.
    assert.deepEqual(server.requests.map((r) => [r.method, r.path]), [["GET", "/api/v1/integrations/context"]]);
    assert.equal(server.requests[0]?.headers["x-nous-integration-grant"], credentials.grantToken);
  } finally {
    await server.close();
  }
});

test("an unavailable selection is an error result, and a missing scope names --context", async () => {
  let reply: { code: number; body: unknown } = {
    code: 200,
    body: { content: [{ error: "context_unavailable" }], is_error: true, source_refs: [] },
  };
  const server = await backend(() => reply);
  try {
    const tool = readSelectedContextTool(new ContextHttpClient(server.origin, credentials));
    const unavailable = await tool.call({});
    assert.equal(unavailable.isError, true);
    assert.match(unavailable.text, /context_unavailable/);
    reply = { code: 403, body: { detail: "Integration access denied" } };
    await assert.rejects(tool.call({}), (error: unknown) => error instanceof ToolRequestRejected && /--context/.test((error as Error).message));
    reply = { code: 200, body: { unexpected: true } };
    await assert.rejects(tool.call({}), /invalid NOUS context result/);
  } finally {
    await server.close();
  }
});

test("the MCP server advertises read_selected_context and no way to change the selection", async () => {
  const server = await backend(() => ({ code: 200, body: [] }));
  try {
    const mcp = createNousMcpServer(new CapabilityClient(server.origin, credentials), [
      readSelectedContextTool(new ContextHttpClient(server.origin, credentials)),
    ]);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await mcp.connect(serverSide);
    const client = new Client({ name: "test", version: "0" });
    await client.connect(clientSide);
    const names = (await client.listTools()).tools.map((t) => t.name);
    assert.deepEqual(names, ["read_selected_context"]);
    await client.close();
  } finally {
    await server.close();
  }
});

test("managed sessions pass --context only for context:read grants, and --context needs --tools", async () => {
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
  assert.equal(args(state).includes("--context"), false);
  state.scopes = ["harness:execute", "tools:read", "context:read"];
  assert.equal(args(state).includes("--context"), true);
  await assert.rejects(
    connect({ stateDir: "/tmp/unused", apiUrl: "https://nous.example/api/v1", projectId: randomUUID(), label: "d", context: true }),
    /--context requires --tools/,
  );
});
