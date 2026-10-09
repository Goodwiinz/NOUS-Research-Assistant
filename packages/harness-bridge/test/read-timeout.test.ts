import assert from "node:assert/strict";
import { test } from "node:test";
import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import {
  CapabilityClient,
  MCP_TOOL_TIMEOUT_SEC,
  READ_TIMEOUT_MS,
  SLOW_READ_TIMEOUT_MS,
} from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";
import { CredentialStore } from "../src/credentials.ts";
import { buildManagedMcpConfig } from "../src/mcp/config.ts";
import { mcpInstallCommand } from "../src/cli.ts";

// RT-2: NOUS bounds search_arxiv at 120 s and get_arxiv_paper_content at 125 s
// (a 120 s budget plus a 5 s outer guard; backend/src/services/integrations/
// read_tools.py), but the bridge aborted every read at 30 s and surfaced the
// abort as a JSON-RPC error, so upstream_timeout and the stale-cache fallback
// never reached Codex.
// Command: pnpm --filter @nous/harness-bridge test test/read-timeout.test.ts

const credentials = { accessToken: "jwt", grantToken: "nous_ig_" + "x".repeat(43) };
const origin = "https://nous.example/api/v1";
const readResult = {
  content: [{ id: "doc-1" }],
  is_error: false,
  source_refs: [{ kind: "document", id: "doc-1" }],
};
const upstreamTimeout = { content: [{ error: "upstream_timeout" }], is_error: true, source_refs: [] };
/** A NOUS that answers after 100 ms, unless the caller has given up by then. */
const slowNous = (async (_input: RequestInfo | URL, init?: RequestInit) => {
  await new Promise((resolve) => setTimeout(resolve, 100));
  init?.signal?.throwIfAborted();
  return new Response(JSON.stringify(readResult), { status: 200 });
}) as typeof fetch;
const invocation = (tool_name: string) => ({ tool_name, arguments: {}, invocation_id: randomUUID() });

test("the production bounds outlast NOUS's slow-tool budget and stay inside Codex's own limit", () => {
  assert.equal(READ_TIMEOUT_MS, 30_000);
  assert.ok(SLOW_READ_TIMEOUT_MS > 125_000, "the client must wait past NOUS's 125 s outer guard");
  assert.ok(MCP_TOOL_TIMEOUT_SEC * 1000 > SLOW_READ_TIMEOUT_MS, "Codex must wait past the client");
});

test("slow arXiv tools get the long bound; other reads time out as upstream_timeout tool errors", async () => {
  const client = new CapabilityClient(origin, credentials, slowNous, { readMs: 30, slowReadMs: 1000 });
  assert.deepEqual(await client.invokeRead(invocation("search_arxiv")), readResult);
  assert.deepEqual(await client.invokeRead(invocation("get_arxiv_paper_content")), readResult);
  assert.deepEqual(await client.invokeRead(invocation("search_documents")), upstreamTimeout);
  // Over MCP it is a tool result the model can act on, not a protocol error.
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const mcp = new Client({ name: "test", version: "0" });
  await createNousMcpServer(client).connect(serverTransport);
  await mcp.connect(clientTransport);
  const result = (await mcp.callTool({ name: "search_documents", arguments: {} })) as {
    isError?: boolean;
    structuredContent?: unknown;
  };
  assert.equal(result.isError, true);
  assert.deepEqual(result.structuredContent, { content: [{ error: "upstream_timeout" }], source_refs: [] });
  await mcp.close();
});

test("a body that stalls past the bound is an upstream_timeout tool error too", async () => {
  // NOUS answers 200 and sends part of the JSON, then stalls until the caller gives up.
  const stalledBody = (async (_input: RequestInfo | URL, init?: RequestInit) =>
    new Response(
      new ReadableStream({
        start(c) {
          c.enqueue(new TextEncoder().encode('{"content": [{"id": "doc'));
          init!.signal!.addEventListener("abort", () => c.error(init!.signal!.reason), { once: true });
        },
      }),
      { status: 200 },
    )) as typeof fetch;
  // AbortSignal.timeout is unref'd: without a ref'd timer the loop could exit first.
  const keepAlive = setTimeout(() => {}, 10_000);
  try {
    const client = new CapabilityClient(origin, credentials, stalledBody, { readMs: 30, slowReadMs: 30 });
    assert.deepEqual(await client.invokeRead(invocation("search_documents")), upstreamTimeout);
  } finally {
    clearTimeout(keepAlive);
  }
});

test("managed Codex sessions get the longer MCP tool timeout; mcp install says what to add", async () => {
  const stateDir = mkdtempSync(join(tmpdir(), "nous-read-timeout-"));
  try {
    const config = buildManagedMcpConfig({ apiOrigin: origin, credentialHandle: randomUUID(), stateDir });
    assert.equal(config.nous?.tool_timeout_sec, MCP_TOOL_TIMEOUT_SEC);
    await new CredentialStore(stateDir).writeLocal("connection", {
      apiUrl: origin,
      deviceId: randomUUID(),
      projectId: randomUUID(),
      credentialHandle: randomUUID(),
      scopes: ["harness:execute", "tools:read"],
      workspaces: [],
    });
    const announced: string[] = [];
    const command = await mcpInstallCommand(stateDir, { announce: (m) => announced.push(m) });
    // stdout stays the bare command; the package never edits ~/.codex/config.toml.
    assert.match(command, /^codex mcp add nous -- /);
    assert.equal(command.includes("tool_timeout_sec"), false);
    assert.ok(
      announced.some((m) => m.includes("[mcp_servers.nous]") && m.includes(`tool_timeout_sec = ${MCP_TOOL_TIMEOUT_SEC}`)),
      announced.join("\n"),
    );
  } finally {
    rmSync(stateDir, { recursive: true, force: true });
  }
});
