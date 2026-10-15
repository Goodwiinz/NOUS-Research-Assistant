import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { ActionHttpClient, ActionStatus } from "../src/actions/client.ts";
import { requestActionTool } from "../src/actions/mcp.ts";
import type { ToolDescriptor, ToolInvocation, ToolResult } from "../src/mcp/client.ts";
import { buildManagedMcpConfig } from "../src/mcp/config.ts";
import { pinCatalog, pinnedArguments } from "../src/mcp/pin.ts";
import { createNousMcpServer, type ToolCatalog } from "../src/mcp/server.ts";

// Hex letters, so the case-insensitive match below is observable.
const PROJECT = "a1b2c3d4-1111-4111-8111-111111111111";
const OTHER = "12121212-1212-4121-8121-121212121212";
const WORKSPACE = "13131313-1313-4131-8131-131313131313";
const DOC = "55555555-5555-4555-8555-555555555555";
const INVOCATION = "44444444-4444-4444-8444-444444444444";
const packageDir = fileURLToPath(new URL("..", import.meta.url));

/** An account grant's catalog as NOUS serves it: project tools take an optional project_id; list_library takes none. */
function accountCatalog(seen: ToolInvocation[]): ToolCatalog {
  const tools: ToolDescriptor[] = [
    {
      name: "search_documents",
      description: "Search",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, project_id: { type: "string", format: "uuid" } },
        required: ["query"],
      },
    },
    {
      name: "get_document_content",
      description: "Read",
      inputSchema: {
        type: "object",
        properties: { document_id: { type: "string" }, project_id: { type: "string", format: "uuid" } },
        required: ["document_id", "project_id"],
      },
    },
    { name: "list_library", description: "List", inputSchema: { type: "object", properties: {} } },
    {
      name: "search_arxiv",
      description: "arXiv",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
    },
  ];
  return {
    listTools: async () => structuredClone(tools),
    invokeRead: async (invocation) => {
      seen.push(invocation);
      return { content: [{ ok: true }], is_error: false, source_refs: [] } as ToolResult;
    },
  };
}

async function connected(catalog: ToolCatalog): Promise<Client> {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test", version: "0" });
  await createNousMcpServer(catalog).connect(serverTransport);
  await client.connect(clientTransport);
  return client;
}
const text = (result: any): string => result.content[0].text;

test("a pinned session fills a missing project_id and refuses another project", async () => {
  const seen: ToolInvocation[] = [];
  const mcp = await connected(pinCatalog(accountCatalog(seen), PROJECT));
  assert.equal((await mcp.callTool({ name: "search_documents", arguments: { query: "q" } })).isError, false);
  assert.deepEqual(seen[0]!.arguments, { query: "q", project_id: PROJECT });
  const same = await mcp.callTool({
    name: "get_document_content",
    arguments: { document_id: DOC, project_id: PROJECT.toUpperCase() },
  });
  assert.equal(same.isError, false);
  const other = await mcp.callTool({ name: "search_documents", arguments: { query: "q", project_id: OTHER } });
  assert.equal(other.isError, true);
  assert.match(text(other), new RegExp(`can use NOUS project ${PROJECT} only.*project_id names another project`));
  await mcp.callTool({ name: "search_arxiv", arguments: { query: "q" } });
  assert.deepEqual(seen.at(-1)!.arguments, { query: "q" }, "a tool without project_id gets none");
  assert.equal(seen.length, 3);
});

test("a pinned catalog hides list_library and offers project_id only as the run's project", async () => {
  const seen: ToolInvocation[] = [];
  const mcp = await connected(pinCatalog(accountCatalog(seen), PROJECT));
  const { tools } = await mcp.listTools();
  assert.deepEqual(tools.map((t) => t.name).sort(), ["get_document_content", "search_arxiv", "search_documents"]);
  const read = tools.find((t) => t.name === "get_document_content")!;
  assert.deepEqual((read.inputSchema.properties as any).project_id.enum, [PROJECT]);
  assert.deepEqual(read.inputSchema.required, ["document_id"]);
  const refused = await mcp.callTool({ name: "list_library", arguments: {} });
  assert.equal(refused.isError, true);
  assert.match(text(refused), /list_library reaches the whole account/);
  assert.equal(seen.length, 0);
});

test("a pinned request_action fills project_id and refuses another project, new folders, moves and metadata edits", async () => {
  const sent: { action: string; args: Record<string, unknown> }[] = [];
  const client = {
    request: async (action: string, _id: string, args: Record<string, unknown>) => {
      sent.push({ action, args });
      return { invocation_id: INVOCATION, state: "succeeded", tool_name: action, result: null, approval_url: null } as ActionStatus;
    },
  } as unknown as ActionHttpClient;
  const tool = requestActionTool(client, PROJECT);
  assert.deepEqual((tool.descriptor.inputSchema.properties as any).project_id.enum, [PROJECT]);
  await tool.call({ action: "save_papers_to_folder", invocation_id: INVOCATION, document_ids: [DOC] });
  await tool.call({ action: "ingest_arxiv_papers", invocation_id: INVOCATION, paper_ids: ["2401.00001"] });
  await tool.call({ action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c" });
  assert.deepEqual(
    sent.map((s) => [s.action, s.args.project_id]),
    [
      ["save_papers_to_folder", PROJECT],
      ["ingest_arxiv_papers", PROJECT],
      ["create_project_note", PROJECT],
    ],
  );
  const other = await tool.call({ action: "delete_folder", invocation_id: INVOCATION, project_id: OTHER });
  assert.equal(other.isError, true);
  assert.match(other.text, /project_id names another project/);
  for (const [action, args] of [
    ["create_folder", { name: "x", workspace_id: WORKSPACE }],
    ["move_papers_between_folders", { document_ids: [DOC], from_project_id: PROJECT, to_project_id: PROJECT }],
    ["update_document_metadata", { document_id: DOC, title: "t" }],
  ] as const) {
    const refused = await tool.call({ action, invocation_id: INVOCATION, ...args });
    assert.equal(refused.isError, true, action);
    assert.match(refused.text, /start Codex yourself/, action);
  }
  assert.equal(sent.length, 3);
  // Every field that names a project is held to the pin, not only project_id.
  for (const field of ["project_id", "from_project_id", "to_project_id"])
    assert.match(String(pinnedArguments(PROJECT, { [field]: OTHER }, true)), /names another project/, field);
});

test("an unpinned request_action sends create_folder's workspace_id and a note's project_id when given", async () => {
  const sent: Record<string, unknown>[] = [];
  const client = {
    request: async (_action: string, _id: string, args: Record<string, unknown>) => {
      sent.push(args);
      return { invocation_id: INVOCATION, state: "succeeded", tool_name: "x", result: null, approval_url: null } as ActionStatus;
    },
  } as unknown as ActionHttpClient;
  const tool = requestActionTool(client);
  await tool.call({ action: "create_folder", invocation_id: INVOCATION, name: "Reading", workspace_id: WORKSPACE });
  await tool.call({ action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c", project_id: OTHER });
  await tool.call({ action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c" });
  assert.deepEqual(sent, [
    { name: "Reading", workspace_id: WORKSPACE },
    { title: "t", content: "c", project_id: OTHER },
    { title: "t", content: "c" },
  ]);
  const bad = await tool.call({ action: "create_folder", invocation_id: INVOCATION, name: "x", workspace_id: "nope" });
  assert.equal(bad.isError, true);
});

test("the MCP child gets --project only as a UUID, and the CLI refuses a malformed one", () => {
  const base = { apiOrigin: "https://nous.test/api/v1", credentialHandle: "h", stateDir: packageDir };
  const args = buildManagedMcpConfig({ ...base, project: PROJECT }).nous!.args;
  assert.equal(args[args.indexOf("--project") + 1], PROJECT);
  assert.equal(buildManagedMcpConfig(base).nous!.args.includes("--project"), false);
  assert.throws(() => buildManagedMcpConfig({ ...base, project: "x" }), /MCP project must be a project UUID/);
  const run = spawnSync(
    process.execPath,
    ["--import", "tsx", "src/cli.ts", "mcp", "--api", "https://nous.test/api/v1", "--session", "h", "--project", "bad"],
    { cwd: packageDir, encoding: "utf8", timeout: 20_000 },
  );
  assert.equal(run.status, 1);
  assert.match(run.stderr, /--project must be a project UUID/);
});
