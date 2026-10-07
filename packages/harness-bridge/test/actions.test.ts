import assert from "node:assert/strict";
import { test } from "node:test";
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type IncomingMessage } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ActionHttpClient, type ActionStatus } from "../src/actions/client.ts";
import { actionStatusTool, describe, requestActionTool } from "../src/actions/mcp.ts";
import { connect, sessionOptionsFor, type LocalState } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";
import { CapabilityClient, ToolRequestRejected } from "../src/mcp/client.ts";
import { buildManagedMcpConfig } from "../src/mcp/config.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";

const credentials = { accessToken: "cli-jwt", grantToken: "nous_ig_" + "a".repeat(43) };
const INVOCATION = "44444444-4444-4444-8444-444444444444";
const APPROVAL = `https://app.example/integrations/actions/${INVOCATION}`;
const DOC = "55555555-5555-4555-8555-555555555555";
const PROJECT = "66666666-6666-4666-8666-666666666666";
const OTHER_PROJECT = "77777777-7777-4777-8777-777777777777";
// Valid arguments for every action NOUS accepts (ALLOWED_ACTIONS in
// backend/src/services/agent/tool_actions.py), each with all of its fields.
const ACTIONS: Record<string, Record<string, unknown>> = {
  create_project_note: { title: "Findings", content: "# Findings", tags: ["a"] },
  save_papers_to_folder: { document_ids: [DOC], project_id: PROJECT },
  remove_papers_from_folder: { document_ids: [DOC], project_id: PROJECT },
  move_papers_between_folders: { document_ids: [DOC], from_project_id: PROJECT, to_project_id: OTHER_PROJECT },
  create_folder: { name: "Reading", description: "Papers to read" },
  rename_folder: { project_id: PROJECT, name: "Read" },
  delete_folder: { project_id: PROJECT },
  update_document_metadata: { document_id: DOC, title: "New title", tags: [] },
  ingest_arxiv_papers: { paper_ids: ["2401.00001"], project_id: PROJECT },
};

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

async function backend(reply: (request: { method: string; path: string }) => { code: number; body: unknown }) {
  const requests: { method: string; path: string; headers: IncomingMessage["headers"]; body: string }[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c) => chunks.push(c));
    request.on("end", () => {
      const seen = { method: request.method ?? "", path: request.url ?? "" };
      requests.push({ ...seen, headers: request.headers, body: Buffer.concat(chunks).toString() });
      const { code, body } = reply(seen);
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

/** Drives the real `nous-harness mcp` child over stdio: list its tools, then make one call. */
async function driveMcpChild(command: string, args: string[], cwd: string, call: { name: string; arguments: Record<string, unknown> }) {
  const child = spawn(command, args, { cwd, env: { ...process.env, HOME: cwd, CODEX_HOME: cwd }, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => (stdout += chunk));
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const send = (message: object) => child.stdin.write(JSON.stringify(message) + "\n");
  const reply = (id: number) =>
    new Promise<Record<string, any>>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`no reply ${id}\n${stderr}`)), 15_000);
      const check = () => {
        for (const line of stdout.split("\n").filter(Boolean)) {
          let message: Record<string, any>;
          try {
            message = JSON.parse(line);
          } catch {
            clearTimeout(timer);
            child.stdout.off("data", check);
            reject(new Error(`non JSON-RPC stdout line: ${line}`));
            return;
          }
          if (message.id === id) {
            clearTimeout(timer);
            child.stdout.off("data", check);
            resolve(message);
            return;
          }
        }
      };
      child.stdout.on("data", check);
      check();
    });
  send({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "0" } },
  });
  await reply(1);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });
  send({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  const tools = (await reply(2)).result.tools as { name: string }[];
  send({ jsonrpc: "2.0", id: 3, method: "tools/call", params: call });
  const result = (await reply(3)).result as { content: { text: string }[]; isError?: boolean };
  child.stdin.end();
  const [code] = (await once(child, "exit")) as [number | null];
  return { tools, result, code, stderr };
}

test("only a succeeded action is described as done", () => {
  const pending = describe(status("awaiting_approval"));
  assert.match(pending, /^Not done yet/);
  assert.ok(pending.includes(APPROVAL));
  assert.match(pending, /do not request it again/);
  for (const state of ["approved", "executing"] as const) assert.doesNotMatch(describe(status(state)), /^Done/);
  assert.match(describe(status("failed", { result: { content: [{ error: "denied by user" }], is_error: true, source_refs: [] } })), /^Not done: .*denied by user/);
  assert.match(describe(status("outcome_unknown")), /Do not retry/);
  assert.match(
    describe(status("succeeded", { result: { content: [{ note_id: "n1" }], is_error: false, source_refs: [] } })),
    /^Done\. .*n1/,
  );
  // A library action's receipt reads the same way.
  const receipt = { ok: true, tool_name: "save_papers_to_folder", project_id: PROJECT, document_ids: [DOC] };
  assert.match(
    describe(status("succeeded", { tool_name: "save_papers_to_folder", result: { content: [receipt], is_error: false, source_refs: [{ document_id: DOC }] } })),
    new RegExp(`^Done\\. .*${DOC}`),
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
    assert.match(outcome.text, /^Not done yet/);
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

test("requestNote is the generic request for create_project_note", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const client = new ActionHttpClient(server.origin, credentials);
    await client.requestNote({ invocationId: INVOCATION, title: "Findings", content: "# Findings" });
    await client.request("create_project_note", INVOCATION, { title: "Findings", content: "# Findings" });
    const [viaNote, viaRequest] = server.requests.map((r) => JSON.parse(r.body));
    assert.deepEqual(viaNote, {
      tool_name: "create_project_note",
      invocation_id: INVOCATION,
      arguments: { title: "Findings", content: "# Findings" },
    });
    assert.deepEqual(viaRequest, viaNote);
  } finally {
    await server.close();
  }
});

test("request_action forwards a library action with only its own fields", async () => {
  const server = await backend(() => ({ code: 200, body: status("succeeded", { tool_name: "save_papers_to_folder" }) }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    const outcome = await tool.call({
      action: "save_papers_to_folder",
      invocation_id: INVOCATION,
      document_ids: [DOC],
      project_id: PROJECT,
      user_id: randomUUID(),
    });
    assert.equal(outcome.isError, undefined);
    assert.match(outcome.text, /^Done\./);
    assert.deepEqual(JSON.parse(server.requests[0]?.body ?? "{}"), {
      tool_name: "save_papers_to_folder",
      invocation_id: INVOCATION,
      arguments: { document_ids: [DOC], project_id: PROJECT },
    });
  } finally {
    await server.close();
  }
});

test("request_action offers every NOUS action and sends each with only its own fields", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    const schema = tool.descriptor.inputSchema as { required: string[]; properties: Record<string, { enum?: string[] }> };
    assert.deepEqual([...(schema.properties.action?.enum ?? [])].sort(), Object.keys(ACTIONS).sort());
    // Only the action and its id are always required; the rest depend on the action.
    assert.deepEqual(schema.required, ["action", "invocation_id"]);
    // Every other action's fields, and identity, ride along and must be dropped.
    const everyField: Record<string, unknown> = Object.assign({}, ...Object.values(ACTIONS));
    const identity = Object.fromEntries(
      ["user_id", "organization_id", "workspace_id", "thread_id", "run_id", "grant_id", "consent_id"].map((key) => [key, randomUUID()]),
    );
    for (const [action, fields] of Object.entries(ACTIONS)) {
      for (const field of Object.keys(fields)) assert.ok(schema.properties[field], `${action}: ${field} is advertised`);
      const invocationId = randomUUID();
      const outcome = await tool.call({ ...everyField, ...identity, ...fields, action, invocation_id: invocationId });
      assert.equal(outcome.isError, undefined, `${action}: ${outcome.text}`);
      assert.deepEqual(
        JSON.parse(server.requests.at(-1)?.body ?? "{}"),
        { tool_name: action, invocation_id: invocationId, arguments: fields },
        action,
      );
    }
    assert.equal(server.requests.length, Object.keys(ACTIONS).length);
  } finally {
    await server.close();
  }
});

test("optional action fields may be left out", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    const cases: [string, Record<string, unknown>][] = [
      ["create_project_note", { title: "t", content: "c" }],
      ["create_folder", { name: "Reading" }],
      ["update_document_metadata", { document_id: DOC, title: "New title" }],
      ["update_document_metadata", { document_id: DOC, tags: ["survey"] }],
      // A project connection's ingest lands in its own project.
      ["ingest_arxiv_papers", { paper_ids: ["2401.00001", "hep-th/9901001"] }],
    ];
    for (const [action, fields] of cases) {
      const outcome = await tool.call({ action, invocation_id: randomUUID(), ...fields });
      assert.equal(outcome.isError, undefined, `${action}: ${outcome.text}`);
      assert.deepEqual(JSON.parse(server.requests.at(-1)?.body ?? "{}").arguments, fields, action);
    }
  } finally {
    await server.close();
  }
});

test("a null optional field NOUS reads as left out is dropped, not refused", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    // NOUS's validators read null as absent for these two fields only
    // (_validate_create_folder, _validate_arxiv_ingest); models filling a flat
    // schema often send null for a field they do not use.
    const cases: [string, Record<string, unknown>, Record<string, unknown>][] = [
      ["create_folder", { name: "Reading", description: null }, { name: "Reading" }],
      ["ingest_arxiv_papers", { paper_ids: ["2401.00001"], project_id: null }, { paper_ids: ["2401.00001"] }],
    ];
    for (const [action, fields, sent] of cases) {
      const outcome = await tool.call({ action, invocation_id: randomUUID(), ...fields });
      assert.equal(outcome.isError, undefined, `${action}: ${outcome.text}`);
      assert.deepEqual(JSON.parse(server.requests.at(-1)?.body ?? "{}").arguments, sent, action);
    }
    assert.equal(server.requests.length, cases.length);
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
      { action: "toString", invocation_id: INVOCATION },
      { invocation_id: INVOCATION, title: "t", content: "c" },
      { action: "create_project_note", invocation_id: "not-a-uuid", title: "t", content: "c" },
      { action: "save_papers_to_folder", document_ids: [DOC], project_id: PROJECT },
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

test("malformed library action fields are refused locally before any request", async () => {
  const server = await backend(() => ({ code: 200, body: status("awaiting_approval") }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    const uuids = (count: number) => Array.from({ length: count }, () => randomUUID());
    const cases: [string, Record<string, unknown>][] = [
      ["save_papers_to_folder", { document_ids: [DOC] }],
      ["save_papers_to_folder", { document_ids: [], project_id: PROJECT }],
      ["save_papers_to_folder", { document_ids: DOC, project_id: PROJECT }],
      ["remove_papers_from_folder", { document_ids: uuids(21), project_id: PROJECT }],
      ["remove_papers_from_folder", { document_ids: ["not-a-uuid"], project_id: PROJECT }],
      ["move_papers_between_folders", { document_ids: [DOC], from_project_id: PROJECT }],
      ["create_folder", { name: "  " }],
      ["create_folder", { name: "Reading", description: 7 }],
      // A folder is chosen by id; a name is never resolved.
      ["rename_folder", { project_id: "Reading", name: "Read" }],
      ["delete_folder", {}],
      ["update_document_metadata", { document_id: DOC }],
      ["update_document_metadata", { document_id: DOC, tags: "survey" }],
      // null is neither "leave unchanged" nor "clear": tags: [] clears them.
      ["update_document_metadata", { document_id: DOC, title: "New title", tags: null }],
      ["update_document_metadata", { document_id: "doc-1", title: "New title" }],
      ["ingest_arxiv_papers", { paper_ids: [] }],
      ["ingest_arxiv_papers", { paper_ids: Array.from({ length: 11 }, (_, i) => `2401.0000${i}`) }],
      ["ingest_arxiv_papers", { paper_ids: [2401.00001] }],
      ["ingest_arxiv_papers", { paper_ids: ["2401.00001"], project_id: "my folder" }],
    ];
    for (const [action, fields] of cases) {
      const outcome = await tool.call({ action, invocation_id: INVOCATION, ...fields });
      assert.equal(outcome.isError, true, `${action} ${JSON.stringify(fields)}`);
      assert.match(outcome.text, new RegExp(`^request_action ${action}: `), outcome.text);
    }
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
    await assert.rejects(
      tool.call(args),
      (error: unknown) =>
        error instanceof ToolRequestRejected && /tools:write/.test(error.message) && /library:write/.test(error.message),
    );
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
    assert.match(done.text, /^Done/);
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

test("a library:write connection is offered the action tools without --actions", async () => {
  const receipt = { ok: true, tool_name: "save_papers_to_folder", project_id: PROJECT, document_ids: [DOC] };
  const server = await backend(({ path }) =>
    path.endsWith("/integrations/tools")
      ? { code: 200, body: [] }
      : {
          code: 200,
          body: status("succeeded", {
            tool_name: "save_papers_to_folder",
            result: { content: [receipt], is_error: false, source_refs: [{ document_id: DOC }] },
          }),
        },
  );
  const stateDir = mkdtempSync(join(tmpdir(), "nous-actions-library-"));
  try {
    const credentialHandle = await new CredentialStore(stateDir).save(credentials);
    // --library alone offers the action tools: the child needs no --actions for them.
    const { command, args } = buildManagedMcpConfig({ apiOrigin: server.origin, credentialHandle, stateDir, library: true }).nous!;
    assert.equal(args.includes("--actions"), false);
    assert.equal(args.includes("--library"), true);
    const run = await driveMcpChild(command, args, stateDir, {
      name: "request_action",
      arguments: { action: "save_papers_to_folder", invocation_id: INVOCATION, document_ids: [DOC], project_id: PROJECT },
    });
    assert.equal(run.code, 0, run.stderr);
    assert.deepEqual(run.tools.map((t) => t.name).sort(), ["get_action_status", "request_action"]);
    assert.equal(run.result.isError, undefined, JSON.stringify(run.result));
    assert.match(run.result.content[0]?.text ?? "", /^Done\./);
    const sent = server.requests.find((r) => r.method === "POST");
    assert.equal(sent?.path, "/api/v1/integrations/actions");
    assert.equal(sent?.headers["x-nous-integration-grant"], credentials.grantToken);
    assert.deepEqual(JSON.parse(sent?.body ?? "{}"), {
      tool_name: "save_papers_to_folder",
      invocation_id: INVOCATION,
      arguments: { document_ids: [DOC], project_id: PROJECT },
    });
  } finally {
    await server.close();
    rmSync(stateDir, { recursive: true, force: true });
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
