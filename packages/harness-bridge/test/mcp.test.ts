import assert from "node:assert/strict";
import { test } from "node:test";
import { spawn } from "node:child_process";
import { createServer, type IncomingMessage } from "node:http";
import { mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative } from "node:path";
import { once } from "node:events";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { CredentialStore } from "../src/credentials.ts";
import {
  CapabilityClient,
  DISABLED_MESSAGE,
  FORBIDDEN_MESSAGE,
  REAUTH_MESSAGE,
  apiBase,
  type McpSession,
  type ToolInvocation,
} from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";
import {
  buildManagedMcpConfig,
  standaloneInstallCommand,
} from "../src/mcp/config.ts";
import {
  addWorkspace,
  connect,
  mcpInstallCommand,
  runBridge,
  sessionOptionsFor,
  type LocalState,
} from "../src/cli.ts";

const cliPath = fileURLToPath(new URL("../src/cli.ts", import.meta.url));
const credentials = {
  accessToken: "jwt",
  grantToken: "nous_ig_" + "x".repeat(43),
};
const catalog = [
  {
    name: "search_documents",
    description: "Search project documents",
    input_schema: {
      type: "object",
      properties: { query: { type: "string" } },
      required: ["query"],
    },
  },
];
const readResult = {
  content: [{ id: "doc-1", title: "Paper" }],
  is_error: false,
  source_refs: [{ kind: "document", id: "doc-1" }],
};
const PROJECT = "11111111-1111-4111-8111-111111111111";
const WORKSPACE = "88888888-8888-4888-8888-888888888888";
const CHAT = "99999999-9999-4999-8999-999999999999";
const DEVICE = "22222222-2222-4222-8222-222222222222";

/** Answers the CLI login, device, consent and exchange calls and records each request. */
function consentFetch(calls: { url: string; body: any }[]): typeof fetch {
  return (async (input: any, init: any) => {
    const url = String(input);
    calls.push({ url, body: init.body ? JSON.parse(init.body) : undefined });
    let data: object;
    if (url.endsWith("/cli-auth/start"))
      data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l" };
    else if (url.includes("/cli-auth/status/")) data = { status: "approved", token: "cli-secret" };
    else if (url.endsWith("/integrations/devices")) data = { id: DEVICE };
    else if (url.endsWith("/grant-requests"))
      data = { id: "33333333-3333-4333-8333-333333333333", approval_url: "https://nous.test/a" };
    else if (url.endsWith("/exchange")) data = { token: "grant-secret" };
    else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
}

/** Records every request the backend receives, including headers and bodies. */
async function recordingBackend(status = 200, readBody: unknown = readResult) {
  const requests: { path: string; headers: IncomingMessage["headers"]; body: string }[] = [];
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => (body += chunk));
    request.on("end", () => {
      requests.push({ path: request.url ?? "", headers: request.headers, body });
      response.writeHead(status, { "Content-Type": "application/json" });
      response.end(
        JSON.stringify(
          status !== 200
            ? { detail: "backend detail" }
            : request.url?.endsWith("/tools")
              ? catalog
              : readBody,
        ),
      );
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  return {
    origin: `http://127.0.0.1:${address.port}/api/v1`,
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

/** Drives a real CLI entry point over stdio with raw JSON-RPC lines. */
async function runEntry(
  command: string,
  args: string[],
  invocation: ToolInvocation,
  env: NodeJS.ProcessEnv,
  cwd: string,
) {
  // cwd is outside the repository: Codex launches from the user's workspace.
  const child = spawn(command, args, { env, cwd, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => (stdout += chunk));
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const send = (message: object) =>
    child.stdin.write(JSON.stringify(message) + "\n");
  const waitFor = (id: number) =>
    new Promise<Record<string, any>>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`no response ${id}\n${stderr}`)),
        15_000,
      );
      const check = () => {
        for (const line of stdout.split("\n").filter(Boolean)) {
          let parsed: Record<string, any>;
          try {
            parsed = JSON.parse(line);
          } catch {
            clearTimeout(timer);
            child.stdout.off("data", check);
            reject(new Error(`non JSON-RPC stdout line: ${line}`));
            return;
          }
          if (parsed.id === id) {
            clearTimeout(timer);
            child.stdout.off("data", check);
            resolve(parsed);
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
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "test", version: "0" },
    },
  });
  await waitFor(1);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });
  send({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  const tools = await waitFor(2);
  send({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: invocation.tool_name, arguments: invocation.arguments },
  });
  const call = await waitFor(3);
  // Closing stdin is how Codex stops an MCP server; the child must exit on its own.
  child.stdin.end();
  const [code] = (await once(child, "exit")) as [number | null];
  assert.equal(code, 0, stderr);
  return {
    tools: tools.result.tools as { name: string }[],
    result: call.result as {
      structuredContent: { source_refs: unknown[] };
      isError: boolean;
    },
    stdoutLines: stdout.split("\n").filter(Boolean),
    stderr,
  };
}

test("managed and standalone tools preserve scope and secrets", async () => {
  const backend = await recordingBackend();
  const stateDir = mkdtempSync(join(tmpdir(), "nous-mcp-"));
  const codexHome = mkdtempSync(join(tmpdir(), "nous-codex-home-"));
  const store = new CredentialStore(stateDir);
  const storedGrant = "nous_ig_" + "g".repeat(43);
  const accessToken = "cli-jwt-secret";
  const credentialHandle = await store.save({ accessToken, grantToken: storedGrant });
  const session: McpSession = { apiOrigin: backend.origin, credentialHandle, stateDir };
  const env = { ...process.env, HOME: codexHome, CODEX_HOME: codexHome };
  const invocation: ToolInvocation = {
    tool_name: "search_documents",
    arguments: { query: "graph" },
    invocation_id: randomUUID(),
  };
  try {
    const config = buildManagedMcpConfig(session);
    const managed = await runEntry(
      config.nous.command,
      config.nous.args,
      invocation,
      env,
      codexHome,
    );
    const standalone = await runEntry(
      process.execPath,
      [
        "--import",
        fileURLToPath(import.meta.resolve("tsx")),
        cliPath,
        "mcp",
        "--api",
        backend.origin,
        "--store",
        stateDir,
        "--session",
        credentialHandle,
      ],
      invocation,
      env,
      codexHome,
    );

    assert.deepEqual(
      managed.result.structuredContent.source_refs,
      standalone.result.structuredContent.source_refs,
    );
    assert.deepEqual(managed.result.structuredContent.source_refs, readResult.source_refs);
    assert.deepEqual(managed.tools.map((t) => t.name), ["search_documents"]);
    for (const request of backend.requests) {
      assert.equal(request.headers["x-nous-integration-grant"], storedGrant);
      assert.equal(request.headers.authorization, `Bearer ${accessToken}`);
    }
    const read = backend.requests.find((r) => r.path.endsWith("/tools/read"));
    assert.ok(read);
    assert.equal(JSON.parse(read.body).tool_name, "search_documents");
    for (const entry of [managed, standalone]) {
      assert.equal(
        entry.stdoutLines.every((line) => JSON.parse(line).jsonrpc === "2.0"),
        true,
      );
    }
    const serialized = JSON.stringify(config);
    assert.equal(serialized.includes(storedGrant), false);
    assert.equal(serialized.includes(accessToken), false);
    assert.equal(serialized.includes(credentialHandle), true);
    const install = standaloneInstallCommand(session);
    assert.equal(install.includes(storedGrant), false);
    assert.equal(install.includes(accessToken), false);
    assert.equal(install.includes(credentialHandle), true);
    // Standalone setup only prints; nothing under CODEX_HOME or HOME is written.
    assert.deepEqual(readdirSync(codexHome), []);
  } finally {
    await backend.close();
    rmSync(stateDir, { recursive: true, force: true });
    rmSync(codexHome, { recursive: true, force: true });
  }
});

test("connect --tools requests and persists tools:read; plain connect does not", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-connect-"));
  const calls: { url: string; body: any }[] = [];
  const fetchFn = (async (input: any, init: any) => {
    const url = String(input);
    calls.push({ url, body: init.body ? JSON.parse(init.body) : undefined });
    let data: object;
    if (url.endsWith("/cli-auth/start"))
      data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l" };
    else if (url.includes("/cli-auth/status/")) data = { status: "approved", token: "cli-secret" };
    else if (url.endsWith("/integrations/devices"))
      data = { id: "22222222-2222-4222-8222-222222222222" };
    else if (url.endsWith("/grant-requests"))
      data = { id: "33333333-3333-4333-8333-333333333333", approval_url: "https://nous.test/a" };
    else if (url.endsWith("/exchange")) data = { token: "grant-secret" };
    else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
  const base = {
    fetchFn,
    announce: () => {},
    apiUrl: "https://nous.test/api/v1",
    projectId: "11111111-1111-4111-8111-111111111111",
    label: "Laptop",
  };
  try {
    await connect({ ...base, stateDir: join(dir, "tools"), tools: true });
    // Reconnecting to add --publish re-registers the existing folders on the new device.
    const upgradeStore = new CredentialStore(join(dir, "publish"));
    const keep = mkdtempSync(join(tmpdir(), "nous-keep-"));
    await upgradeStore.writeLocal("connection", {
      apiUrl: base.apiUrl,
      deviceId: "44444444-4444-4444-8444-444444444444",
      projectId: base.projectId,
      credentialHandle: "55555555-5555-4555-8555-555555555555",
      scopes: ["harness:execute", "tools:read"],
      workspaces: [
        { id: "66666666-6666-4666-8666-666666666666", root: keep, label: "Kept", projectId: base.projectId },
        { id: "77777777-7777-4777-8777-777777777777", root: join(keep, "vanished"), label: "Gone", projectId: base.projectId },
      ],
    });
    await connect({ ...base, stateDir: join(dir, "publish"), tools: true, publish: true });
    const registrations = calls.filter((c) => c.url.endsWith("/workspaces"));
    assert.equal(registrations.length, 1);
    assert.equal(registrations[0]?.body.label, "Kept");
    const upgraded = JSON.parse(readFileSync(join(dir, "publish", "connection.json"), "utf8"));
    assert.equal(upgraded.workspaces.length, 1);
    assert.equal(upgraded.workspaces[0].root, realpathSync(keep));
    assert.notEqual(upgraded.workspaces[0].id, "66666666-6666-4666-8666-666666666666");
    rmSync(keep, { recursive: true, force: true });
    const withPublish = calls.filter((c) => c.url.endsWith("/grant-requests")).at(-1)!;
    assert.deepEqual(withPublish.body.scopes, ["harness:execute", "tools:read", "artifacts:publish"]);
    await assert.rejects(connect({ ...base, stateDir: join(dir, "bad"), publish: true }), /--publish requires --tools/);
    const withTools = calls.find((c) => c.url.endsWith("/grant-requests"))!;
    assert.deepEqual(withTools.body.scopes, ["harness:execute", "tools:read"]);
    const state = JSON.parse(readFileSync(join(dir, "tools", "connection.json"), "utf8"));
    assert.deepEqual(state.scopes, ["harness:execute", "tools:read"]);
    assert.ok(await mcpInstallCommand(join(dir, "tools")));

    calls.length = 0;
    await connect({ ...base, stateDir: join(dir, "plain") });
    const plain = calls.find((c) => c.url.endsWith("/grant-requests"))!;
    assert.deepEqual(plain.body.scopes, ["harness:execute"]);
    await assert.rejects(mcpInstallCommand(join(dir, "plain")), /reconnect with --tools/);

    // Connections written before this feature have no scopes key at all.
    const store = new CredentialStore(join(dir, "legacy"));
    const legacy = JSON.parse(readFileSync(join(dir, "plain", "connection.json"), "utf8"));
    delete legacy.scopes;
    await store.writeLocal("connection", legacy);
    await assert.rejects(mcpInstallCommand(join(dir, "legacy")), /reconnect with --tools/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("managed sessions get the MCP server only with tools:read, with an absolute store", () => {
  const stateDir = mkdtempSync(join(tmpdir(), "nous-mcp-run-"));
  const relativeDir = relative(process.cwd(), stateDir);
  assert.equal(isAbsolute(relativeDir), false);
  const workspace = { id: randomUUID(), root: stateDir, label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    projectId: workspace.projectId,
    credentialHandle: randomUUID(),
    workspaces: [workspace],
  };
  try {
    assert.equal("mcpConfig" in sessionOptionsFor(stateDir, state, workspace.id), false);
    state.scopes = ["harness:execute"];
    assert.equal("mcpConfig" in sessionOptionsFor(stateDir, state, workspace.id), false);
    state.scopes = ["harness:execute", "tools:read"];
    const options = sessionOptionsFor(relativeDir, state, workspace.id);
    const args = options.mcpConfig?.nous?.args ?? [];
    assert.ok(args.includes(state.credentialHandle));
    const store = args[args.indexOf("--store") + 1];
    assert.equal(store, stateDir);
    assert.ok(isAbsolute(args[args.indexOf("--import") + 1]!));
  } finally {
    rmSync(stateDir, { recursive: true, force: true });
  }
});

test("connect --workspace --library requests a workspace binding and the library scopes, never harness:execute", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-workspace-"));
  const calls: { url: string; body: any }[] = [];
  const messages: string[] = [];
  try {
    await connect({
      stateDir: dir,
      fetchFn: consentFetch(calls),
      announce: (message) => messages.push(message),
      apiUrl: "https://nous.test/api/v1",
      workspaceId: WORKSPACE,
      label: "dev",
      tools: true,
      write: true,
      library: true,
    });
    const consent = calls.find((c) => c.url.endsWith("/grant-requests"))!;
    // Harness runs stay project-bound; the backend refuses harness:execute for a workspace grant.
    assert.deepEqual(consent.body, {
      workspace_id: WORKSPACE,
      device_id: DEVICE,
      scopes: ["tools:read", "tools:write", "library:read", "library:write"],
    });
    // There is no project to register local folders against.
    assert.equal(calls.some((c) => c.url.endsWith("/workspaces")), false);
    const state = JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.equal(state.workspaceId, WORKSPACE);
    assert.equal("projectId" in state, false);
    assert.deepEqual(state.scopes, ["tools:read", "tools:write", "library:read", "library:write"]);
    assert.deepEqual(state.workspaces, []);
    assert.equal(messages.some((m) => m.includes("device and workspace")), true);
    assert.equal(messages.at(-1), `Connected to workspace ${WORKSPACE}.`);
    // The standalone MCP command knows the grant can write the library and is bound to no folder.
    const install = await mcpInstallCommand(dir);
    assert.equal(install.includes("'--actions'"), true);
    assert.equal(install.includes("'--library'"), true);
    assert.equal(install.includes("'--root'"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("connect --project --library keeps harness:execute and puts the library scopes last", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-project-library-"));
  const calls: { url: string; body: any }[] = [];
  try {
    await connect({
      stateDir: dir,
      fetchFn: consentFetch(calls),
      announce: () => {},
      apiUrl: "https://nous.test/api/v1",
      projectId: PROJECT,
      label: "dev",
      tools: true,
      write: true,
      library: true,
    });
    const consent = calls.find((c) => c.url.endsWith("/grant-requests"))!;
    assert.deepEqual(consent.body, {
      project_id: PROJECT,
      device_id: DEVICE,
      scopes: ["harness:execute", "tools:read", "tools:write", "library:read", "library:write"],
    });
    const state = JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.equal(state.projectId, PROJECT);
    assert.equal("workspaceId" in state, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("connect refuses contradictory binding and scope flags before any request", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-flags-"));
  const calls: { url: string; body: any }[] = [];
  const base = {
    stateDir: dir,
    fetchFn: consentFetch(calls),
    announce: () => {},
    apiUrl: "https://nous.test/api/v1",
    label: "dev",
  };
  const project = { ...base, projectId: PROJECT };
  const workspace = { ...base, workspaceId: WORKSPACE };
  try {
    for (const bound of [project, workspace]) {
      await assert.rejects(connect({ ...bound, tools: false, library: true }), /--library requires --tools/);
      await assert.rejects(
        connect({ ...bound, tools: true, write: false, library: true }),
        /--library requires --write/,
      );
    }
    await assert.rejects(
      connect({ ...workspace, tools: true, publish: true }),
      /--workspace cannot be combined with --publish/,
    );
    // Without tools:read a workspace grant could do nothing, and NOUS refuses an empty scope set.
    await assert.rejects(connect({ ...workspace }), /--workspace requires --tools/);
    await assert.rejects(connect({ ...base, tools: true }), /--project or --workspace \(not both\) required/);
    await assert.rejects(
      connect({ ...base, projectId: PROJECT, workspaceId: WORKSPACE, tools: true }),
      /--project or --workspace \(not both\) required/,
    );
    await assert.rejects(
      connect({ ...workspace, workspaceId: "not-a-uuid", tools: true }),
      /workspace UUID and device label required/,
    );
    await assert.rejects(
      connect({ ...project, projectId: "not-a-uuid", tools: true }),
      /project UUID and device label required/,
    );
    // A chat belongs to one project, so it cannot bind a workspace grant.
    await assert.rejects(connect({ ...workspace, tools: true, threadId: CHAT }), /--chat requires --project/);
    // A handoff lives in one chat of one project; a workspace grant has neither.
    await assert.rejects(
      connect({ ...workspace, tools: true, handoff: true }),
      /--workspace cannot be combined with --handoff/,
    );
    assert.equal(calls.length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("--handoff and --library each reach the MCP child argv on their own scope", () => {
  const folder = { id: randomUUID(), root: "/tmp", label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    projectId: folder.projectId,
    threadId: CHAT,
    credentialHandle: randomUUID(),
    scopes: ["harness:execute", "tools:read"],
    workspaces: [folder],
  };
  const flags = (...scopes: string[]) => {
    const args = sessionOptionsFor("/tmp/state", { ...state, scopes: ["harness:execute", "tools:read", ...scopes] }, folder.id)
      .mcpConfig?.nous?.args ?? [];
    return { handoff: args.includes("--handoff"), library: args.includes("--library") };
  };
  assert.deepEqual(flags(), { handoff: false, library: false });
  assert.deepEqual(flags("handoff:read", "handoff:write"), { handoff: true, library: false });
  assert.deepEqual(flags("tools:write", "library:read", "library:write"), { handoff: false, library: true });
  assert.deepEqual(
    flags("tools:write", "handoff:read", "handoff:write", "library:read", "library:write"),
    { handoff: true, library: true },
  );
});

test("the nous-harness CLI parses --workspace and --library into the consent request", async () => {
  const requests: { path: string; body: any }[] = [];
  const server = createServer((request, response) => {
    let raw = "";
    request.on("data", (chunk) => (raw += chunk));
    request.on("end", () => {
      const path = request.url ?? "";
      requests.push({ path, body: raw ? JSON.parse(raw) : undefined });
      let data: object = { status: "approved" };
      if (path.endsWith("/cli-auth/start"))
        data = { session_id: "s", poll_token: "p", browser_url: "https://nous.test/l" };
      else if (path.includes("/cli-auth/status/")) data = { status: "approved", token: "cli-secret" };
      else if (path.endsWith("/integrations/devices")) data = { id: DEVICE };
      else if (path.endsWith("/grant-requests"))
        data = { id: "33333333-3333-4333-8333-333333333333", approval_url: "https://nous.test/a" };
      else if (path.endsWith("/exchange")) data = { token: "grant-secret" };
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(data));
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  const api = `http://127.0.0.1:${address.port}/api/v1`;
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-cli-"));
  const cwd = mkdtempSync(join(tmpdir(), "nous-mcp-cli-cwd-"));
  const cli = async (...args: string[]) => {
    const child = spawn(
      process.execPath,
      ["--import", fileURLToPath(import.meta.resolve("tsx")), cliPath, ...args, "--api", api, "--store", dir],
      { cwd, stdio: ["ignore", "pipe", "pipe"] },
    );
    let stderr = "";
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.stdout.resume();
    const [code] = (await once(child, "exit")) as [number | null];
    return { code, stderr };
  };
  try {
    const both = await cli("connect", "--project", PROJECT, "--workspace", WORKSPACE, "--label", "dev", "--tools");
    assert.equal(both.code, 1);
    assert.match(both.stderr, /--project or --workspace \(not both\) required/);
    assert.equal(requests.length, 0);

    const ok = await cli("connect", "--workspace", WORKSPACE, "--label", "dev", "--tools", "--write", "--library");
    assert.equal(ok.code, 0, ok.stderr);
    const consent = requests.find((r) => r.path.endsWith("/grant-requests"))!;
    assert.deepEqual(consent.body, {
      workspace_id: WORKSPACE,
      device_id: DEVICE,
      scopes: ["tools:read", "tools:write", "library:read", "library:write"],
    });
    const state = JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.equal(state.workspaceId, WORKSPACE);
    assert.equal("projectId" in state, false);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    rmSync(dir, { recursive: true, force: true });
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("reconnecting to a workspace does not re-register folders against a missing project", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-workspace-carry-"));
  const keep = mkdtempSync(join(tmpdir(), "nous-keep-"));
  const calls: { url: string; body: any }[] = [];
  const messages: string[] = [];
  try {
    await new CredentialStore(dir).writeLocal("connection", {
      apiUrl: "https://nous.test/api/v1",
      deviceId: "44444444-4444-4444-8444-444444444444",
      projectId: PROJECT,
      credentialHandle: "55555555-5555-4555-8555-555555555555",
      scopes: ["harness:execute", "tools:read"],
      workspaces: [{ id: "66666666-6666-4666-8666-666666666666", root: keep, label: "Kept", projectId: PROJECT }],
    } satisfies LocalState);
    await connect({
      stateDir: dir,
      fetchFn: consentFetch(calls),
      announce: (message) => messages.push(message),
      apiUrl: "https://nous.test/api/v1",
      workspaceId: WORKSPACE,
      label: "dev",
      tools: true,
    });
    assert.equal(calls.some((c) => c.url.endsWith("/workspaces")), false);
    const state = JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.deepEqual(state.workspaces, []);
    assert.equal(state.workspaceId, WORKSPACE);
    assert.equal("projectId" in state, false);
    // The old folders are dropped loudly, never silently.
    assert.equal(messages.some((m) => /not carried over.*MCP-only.*--project/.test(m)), true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(keep, { recursive: true, force: true });
  }
});

test("workspace connections are MCP-only: no managed sessions, folders, bridge run or output root", async () => {
  const stateDir = mkdtempSync(join(tmpdir(), "nous-mcp-workspace-state-"));
  const folder = { id: randomUUID(), root: stateDir, label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    workspaceId: WORKSPACE,
    credentialHandle: randomUUID(),
    scopes: ["tools:read"],
    workspaces: [folder],
  };
  try {
    assert.throws(
      () => sessionOptionsFor(stateDir, state, folder.id),
      /workspace connections are MCP-only; reconnect with --project to run harness sessions/,
    );
    // Even a hand-edited state that claims artifacts:publish and a folder gets no output root.
    const forged = { ...state, scopes: ["tools:read", "artifacts:publish"] };
    await new CredentialStore(stateDir).writeLocal("connection", forged);
    const command = await mcpInstallCommand(stateDir, { announce: () => {} });
    assert.equal(command.includes("'--root'"), false);
    // Neither folder registration nor the bridge loop can start from a workspace connection.
    await assert.rejects(
      addWorkspace({ stateDir, root: stateDir }),
      /workspace connections are MCP-only; reconnect with --project to register local folders/,
    );
    // Bounded, so a missing guard fails the assertion instead of looping forever.
    const stop = new AbortController();
    const timer = setTimeout(() => stop.abort(), 2000);
    try {
      await assert.rejects(
        runBridge(stateDir, stop.signal),
        /workspace connections are MCP-only; reconnect with --project to run harness sessions/,
      );
    } finally {
      clearTimeout(timer);
    }
  } finally {
    rmSync(stateDir, { recursive: true, force: true });
  }
});

test("library:write adds --library to the MCP child argv, and the CLI accepts it", async () => {
  const backend = await recordingBackend();
  const stateDir = mkdtempSync(join(tmpdir(), "nous-mcp-library-"));
  const codexHome = mkdtempSync(join(tmpdir(), "nous-codex-home-"));
  const folder = { id: randomUUID(), root: stateDir, label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    projectId: folder.projectId,
    credentialHandle: randomUUID(),
    scopes: ["harness:execute", "tools:read", "tools:write"],
    workspaces: [folder],
  };
  const args = (s: LocalState) => sessionOptionsFor(stateDir, s, folder.id).mcpConfig?.nous?.args ?? [];
  try {
    // tools:write alone is not library access.
    assert.equal(args(state).includes("--actions"), true);
    assert.equal(args(state).includes("--library"), false);
    state.scopes = [...state.scopes!, "library:read", "library:write"];
    assert.equal(args(state).includes("--library"), true);
    // Without the write scope the flag is never emitted, whatever else is granted.
    state.scopes = ["harness:execute", "tools:read", "library:read"];
    assert.equal(args(state).includes("--library"), false);

    const credentialHandle = await new CredentialStore(stateDir).save({
      accessToken: "cli-jwt-secret",
      grantToken: "nous_ig_" + "g".repeat(43),
    });
    const session: McpSession = {
      apiOrigin: backend.origin,
      credentialHandle,
      stateDir,
      actions: true,
      library: true,
    };
    const config = buildManagedMcpConfig(session);
    assert.equal(config.nous.args.includes("--library"), true);
    assert.equal(standaloneInstallCommand(session).includes("'--library'"), true);
    const { library: _library, ...withoutLibrary } = session;
    assert.equal(buildManagedMcpConfig(withoutLibrary).nous.args.includes("--library"), false);
    assert.equal(standaloneInstallCommand(withoutLibrary).includes("--library"), false);
    // The argv we emit must start a working server: the CLI parses its options strictly.
    const run = await runEntry(
      config.nous.command,
      config.nous.args,
      { tool_name: "search_documents", arguments: { query: "graph" }, invocation_id: randomUUID() },
      { ...process.env, HOME: codexHome, CODEX_HOME: codexHome },
      codexHome,
    );
    assert.deepEqual(run.tools.map((t) => t.name).sort(), [
      "get_action_status",
      "request_action",
      "search_documents",
    ]);
  } finally {
    await backend.close();
    rmSync(stateDir, { recursive: true, force: true });
    rmSync(codexHome, { recursive: true, force: true });
  }
});

test("caller arguments cannot override identity, headers, or the API origin", async () => {
  const backend = await recordingBackend();
  try {
    const client = new CapabilityClient(backend.origin, credentials);
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const mcp = new Client({ name: "test", version: "0" });
    await createNousMcpServer(client).connect(serverTransport);
    await mcp.connect(clientTransport);
    await mcp.callTool({
      name: "search_documents",
      arguments: {
        query: "x",
        user_id: "attacker",
        headers: { Authorization: "Bearer other" },
        apiOrigin: "https://evil.example",
      },
    });
    const read = backend.requests.find((r) => r.path.endsWith("/tools/read"));
    assert.ok(read);
    assert.equal(read.headers.authorization, "Bearer jwt");
    assert.equal(read.headers.host?.startsWith("127.0.0.1"), true);
    // Model-supplied arguments travel only inside the invocation body; the
    // backend rejects unknown keys, and nothing here rewrites the request.
    const body = JSON.parse(read.body);
    assert.deepEqual(Object.keys(body).sort(), ["arguments", "invocation_id", "tool_name"]);
    await mcp.close();
  } finally {
    await backend.close();
  }
});

for (const [status, expected] of [
  [401, REAUTH_MESSAGE],
  [403, FORBIDDEN_MESSAGE],
  [422, "NOUS rejected the tool arguments: backend detail"],
  [503, "backend detail"],
] as const)
  test(`gateway ${status} becomes a stable tool error without retry`, async () => {
    const backend = await recordingBackend(status);
    try {
      const client = new CapabilityClient(backend.origin, credentials);
      const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
      const mcp = new Client({ name: "test", version: "0" });
      await createNousMcpServer(client).connect(serverTransport);
      await mcp.connect(clientTransport);
      const result = (await mcp.callTool({
        name: "search_documents",
        arguments: { query: "x" },
      })) as { isError?: boolean; content: { type: string; text: string }[] };
      assert.equal(result.isError, true);
      assert.equal(result.content[0]?.text, expected);
      assert.equal(backend.requests.length, 1);
      await mcp.close();
    } finally {
      await backend.close();
    }
  });

test("a 503 without detail still names the disabled gateway", async () => {
  const client = new CapabilityClient(
    "https://nous.example/api/v1",
    credentials,
    (async () => new Response("", { status: 503 })) as typeof fetch,
  );
  await assert.rejects(client.listTools(), { message: DISABLED_MESSAGE });
});

test("malformed tool results and unreachable hosts are loud, not empty successes", async () => {
  const backend = await recordingBackend(200, { ok: true });
  try {
    const client = new CapabilityClient(backend.origin, credentials);
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const mcp = new Client({ name: "test", version: "0" });
    await createNousMcpServer(client).connect(serverTransport);
    await mcp.connect(clientTransport);
    await assert.rejects(
      mcp.callTool({ name: "search_documents", arguments: { query: "x" } }),
      /invalid NOUS tool result/,
    );
    await mcp.close();
  } finally {
    await backend.close();
  }
  // backend is closed now, so its port refuses connections.
  const unreachable = new CapabilityClient(backend.origin, credentials);
  await assert.rejects(unreachable.listTools(), /127\.0\.0\.1:\d+.*ECONNREFUSED/);
});

test("only HTTPS or the loopback development origin is accepted", () => {
  assert.equal(apiBase("https://nous.example/api/v1/"), "https://nous.example/api/v1");
  assert.equal(apiBase("http://localhost:8000/api/v1"), "http://localhost:8000/api/v1");
  assert.throws(() => apiBase("http://nous.example/api/v1"));
  assert.throws(() => apiBase("https://user:pw@nous.example/api/v1"));
});
