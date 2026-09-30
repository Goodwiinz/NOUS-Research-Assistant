import assert from "node:assert/strict";
import { test } from "node:test";
import { createHash, randomUUID } from "node:crypto";
import { createServer, type IncomingMessage } from "node:http";
import { linkSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ArtifactHttpClient } from "../src/artifacts/client.ts";
import type {
  ArtifactApiClient,
  ArtifactUploadDTO,
  ArtifactVersionDTO,
  PublishVersionRequest,
  ReserveArtifactUploadRequest,
} from "../src/artifacts/contracts.ts";
import { artifactsPublishTool } from "../src/artifacts/mcp.ts";
import { createArtifactPublisher } from "../src/artifacts/publisher.ts";
import { MAX_SNAPSHOT_BYTES, grantedRoot, readSnapshot } from "../src/artifacts/snapshot.ts";
import { CapabilityClient, ToolRequestRejected } from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";
import { mcpInstallCommand, sessionOptionsFor, type LocalState } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";
import { renameSync, chmodSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { buildManagedMcpConfig } from "../src/mcp/config.ts";

const CONTENT = Buffer.from("report\n");
const DIGEST = createHash("sha256").update(CONTENT).digest("hex");
const credentials = { accessToken: "jwt", grantToken: "nous_ig_" + "x".repeat(43) };

function version(id: string): ArtifactVersionDTO {
  return {
    artifact_id: randomUUID(),
    version_id: id,
    parent_version_id: null,
    title: "report.md",
    mime_type: "text/markdown",
    byte_size: CONTENT.length,
    sha256: DIGEST,
    created_at: new Date().toISOString(),
    provenance: { producer: "harness", source_ids: [] },
  };
}

/** Fake backend that is idempotent by publication id, like the real one. */
function fakeClient() {
  const calls = { reserve: [] as ReserveArtifactUploadRequest[], upload: [] as string[], finalize: [] as PublishVersionRequest[] };
  const versions = new Map<string, ArtifactVersionDTO>();
  const reserveHash = new Map<string, string>();
  const finalizeHash = new Map<string, string>();
  const hashOf = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
  // Like the backend: same publication_id with a different payload is a 409.
  const client: ArtifactApiClient = {
    async reserve(request) {
      calls.reserve.push(request);
      const seen = reserveHash.get(request.publication_id);
      if (seen !== undefined && seen !== hashOf(request)) throw new ToolRequestRejected("Artifact publication conflict");
      reserveHash.set(request.publication_id, hashOf(request));
      return { upload_id: "11111111-1111-4111-8111-" + request.publication_id.slice(-12), expires_at: new Date().toISOString() } as ArtifactUploadDTO;
    },
    async upload(id, bytes) {
      calls.upload.push(id + ":" + createHash("sha256").update(bytes).digest("hex"));
    },
    async finalize(request) {
      calls.finalize.push(request);
      const seen = finalizeHash.get(request.publication_id);
      if (seen !== undefined && seen !== hashOf(request)) throw new ToolRequestRejected("Artifact publication conflict");
      finalizeHash.set(request.publication_id, hashOf(request));
      let existing = versions.get(request.publication_id);
      if (!existing) {
        existing = version(randomUUID());
        versions.set(request.publication_id, existing);
      }
      return existing;
    },
  };
  return { client, calls };
}

function workspace() {
  // The root is a child of `outside`, so `../secret.txt` names a real file.
  const outside = realpathSync(mkdtempSync(join(tmpdir(), "nous-outside-")));
  const dir = join(outside, "root");
  mkdirSync(dir);
  writeFileSync(join(dir, "report.md"), CONTENT);
  mkdirSync(join(dir, "nested"));
  writeFileSync(join(dir, "nested", "chart.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  writeFileSync(join(outside, "secret.txt"), "secret\n");
  symlinkSync(join(outside, "secret.txt"), join(dir, "link.md"));
  symlinkSync(outside, join(dir, "linkdir"));
  writeFileSync(join(dir, "hard.md"), CONTENT);
  linkSync(join(dir, "hard.md"), join(outside, "hard-alias.md"));
  writeFileSync(join(dir, "big.bin"), Buffer.alloc(MAX_SNAPSHOT_BYTES + 1));
  return { dir, outside, cleanup: () => rmSync(outside, { recursive: true, force: true }) };
}

test("rejects escaping or unsafe paths before any upload", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    for (const relativePath of ["../secret.txt", "/etc/passwd", "nested/../../x", "link.md", "linkdir/secret.txt", "hard.md", "nested", "", "a\u0000b"]) {
      await assert.rejects(publisher.publish({ relativePath, title: "x", publicationId: randomUUID() }), { code: "unsafe_path" }, relativePath);
    }
    await assert.rejects(publisher.publish({ relativePath: "big.bin", title: "x", publicationId: randomUUID() }), { code: "too_large" });
    await assert.rejects(publisher.publish({ relativePath: "missing.md", title: "x", publicationId: randomUUID() }), { code: "not_found" });
    assert.equal(calls.reserve.length, 0);
    assert.equal(calls.upload.length, 0);
  } finally {
    ws.cleanup();
  }
});

test("snapshot is stable, digest-verified, and root identity is pinned", async () => {
  const ws = workspace();
  try {
    const root = await grantedRoot(ws.dir);
    const bytes = await readSnapshot(root, "report.md");
    assert.equal(createHash("sha256").update(bytes).digest("hex"), DIGEST);
    const nested = await readSnapshot(root, "nested/chart.png");
    assert.equal(nested.length, 4);
    // A root whose identity no longer matches the granted one is refused.
    const moved = { ...root, inode: root.inode + 1 };
    await assert.rejects(readSnapshot(moved, "report.md"), { code: "unsafe_path" });
  } finally {
    ws.cleanup();
  }
});

test("publish sequences reserve, upload and finalize and sends the same publication_id on retry", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    const publicationId = randomUUID();
    const first = await publisher.publish({ relativePath: "report.md", title: "Report", publicationId });
    const second = await publisher.publish({ relativePath: "report.md", title: "Report", publicationId });
    assert.equal(first.version_id, second.version_id);
    assert.deepEqual(calls.reserve[0], { publication_id: publicationId, byte_size: CONTENT.length, mime_type: "text/markdown", sha256: DIGEST });
    assert.equal(calls.upload[0], "11111111-1111-4111-8111-" + publicationId.slice(-12) + ":" + DIGEST);
    assert.equal(calls.finalize.length, 2);
    assert.equal(calls.finalize[1]?.publication_id, publicationId);
    assert.equal(calls.finalize[0]?.title, "Report");
    assert.equal(calls.finalize[0]?.provenance.producer, "harness");
    assert.equal(calls.finalize[0]?.upload_id, "11111111-1111-4111-8111-" + publicationId.slice(-12));
    const png = await publisher.publish({ relativePath: "nested/chart.png", title: "Chart", publicationId: randomUUID() });
    assert.ok(png.version_id);
    assert.equal(calls.reserve[2]?.mime_type, "image/png");
  } finally {
    ws.cleanup();
  }
});

test("MCP exposes artifacts_publish beside the catalog and never takes the root from arguments", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    const read = new CapabilityClient("http://127.0.0.1:1/api/v1", credentials, (async () =>
      new Response(JSON.stringify([{ name: "search_documents", description: "d", input_schema: { type: "object" } }]), { status: 200 })) as typeof fetch);
    const server = createNousMcpServer(read, [artifactsPublishTool(publisher)]);
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const mcp = new Client({ name: "test", version: "0" });
    await server.connect(serverTransport);
    await mcp.connect(clientTransport);
    const listed = await mcp.listTools();
    assert.deepEqual(listed.tools.map((t) => t.name).sort(), ["artifacts_publish", "search_documents"]);
    const publicationId = randomUUID();
    const result = (await mcp.callTool({
      name: "artifacts_publish",
      arguments: { relative_path: "report.md", title: "Report", publication_id: publicationId, root: ws.outside, relativePath: "../x" },
    })) as { isError?: boolean; structuredContent?: { version_id: string } };
    assert.equal(result.isError, undefined);
    assert.ok(result.structuredContent?.version_id);
    assert.equal(calls.reserve[0]?.publication_id, publicationId);
    assert.equal(calls.reserve[0]?.sha256, DIGEST); // bytes came from the granted root
    const bad = (await mcp.callTool({ name: "artifacts_publish", arguments: { relative_path: "../secret.txt", title: "x", publication_id: randomUUID() } })) as { isError?: boolean; content: { text: string }[] };
    assert.equal(bad.isError, true);
    assert.match(bad.content[0]?.text ?? "", /unsafe_path/);
    const missing = (await mcp.callTool({ name: "artifacts_publish", arguments: { title: "x" } })) as { isError?: boolean };
    assert.equal(missing.isError, true);
    await mcp.close();
  } finally {
    ws.cleanup();
  }
});

test("HTTP client sends grant headers, raw bytes with a length, and maps gateway errors", async () => {
  const requests: { method: string; path: string; headers: IncomingMessage["headers"]; body: Buffer }[] = [];
  let status = 200;
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c) => chunks.push(c));
    request.on("end", () => {
      requests.push({ method: request.method ?? "", path: request.url ?? "", headers: request.headers, body: Buffer.concat(chunks) });
      response.writeHead(status, { "Content-Type": "application/json" });
      if (status !== 200) return response.end(JSON.stringify({ detail: "publication paused for maintenance" }));
      if (request.url?.endsWith("/uploads")) return response.end(JSON.stringify({ upload_id: "22222222-2222-4222-8222-222222222222", expires_at: new Date().toISOString() }));
      if (request.method === "PUT") { response.statusCode = 204; return response.end(); }
      return response.end(JSON.stringify(version("33333333-3333-4333-8333-333333333333")));
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  const client = new ArtifactHttpClient(`http://127.0.0.1:${address.port}/api/v1`, credentials);
  try {
    const upload = await client.reserve({ publication_id: randomUUID(), byte_size: 7, mime_type: "text/markdown", sha256: DIGEST });
    await client.upload(upload.upload_id, new Uint8Array(CONTENT));
    const finalized = await client.finalize({ publication_id: randomUUID(), upload_id: upload.upload_id, title: "Report", provenance: { producer: "harness" } });
    assert.equal(finalized.version_id, "33333333-3333-4333-8333-333333333333");
    assert.deepEqual(requests.map((r) => [r.method, r.path]), [
      ["POST", "/api/v1/artifacts/uploads"],
      ["PUT", "/api/v1/artifacts/uploads/22222222-2222-4222-8222-222222222222/content"],
      ["POST", "/api/v1/artifacts/versions"],
    ]);
    for (const request of requests) assert.equal(request.headers["x-nous-integration-grant"], credentials.grantToken);
    assert.equal(requests[1]?.headers["content-type"], "application/octet-stream");
    assert.equal(requests[1]?.headers["content-length"], "7");
    assert.ok(requests[1]?.body.equals(CONTENT));
    status = 503;
    await assert.rejects(client.reserve({ publication_id: randomUUID(), byte_size: 7, mime_type: "text/markdown", sha256: DIGEST }), /publication paused for maintenance/);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("managed sessions bind the registered root only when artifacts:publish was granted", () => {
  const ws = workspace();
  try {
    const workspaceBinding = { id: randomUUID(), root: ws.dir, label: "w", projectId: randomUUID() };
    const state: LocalState = {
      apiUrl: "https://nous.example/api/v1",
      deviceId: randomUUID(),
      projectId: workspaceBinding.projectId,
      credentialHandle: randomUUID(),
      scopes: ["harness:execute", "tools:read"],
      workspaces: [workspaceBinding],
    };
    const args = (s: LocalState) => sessionOptionsFor(ws.dir, s, workspaceBinding.id).mcpConfig?.nous?.args ?? [];
    assert.equal(args(state).includes("--root"), false);
    state.scopes = ["harness:execute", "tools:read", "artifacts:publish"];
    const withRoot = args(state);
    assert.equal(withRoot[withRoot.indexOf("--root") + 1], ws.dir);
    assert.equal(withRoot.includes(state.credentialHandle), true);
  } finally {
    ws.cleanup();
  }
});

test("an ancestor swapped for a symlink after the path checks is still refused", async () => {
  const ws = workspace();
  try {
    mkdirSync(join(ws.dir, "a"));
    writeFileSync(join(ws.dir, "a", "notes.md"), CONTENT);
    writeFileSync(join(ws.outside, "notes.md"), "secret\n");
    const root = await grantedRoot(ws.dir);
    // Model swaps root/a for a symlink to `outside` after the ancestor checks, before the
    // final lstat and open, so the pre/post identity check alone cannot catch it.
    const swap = async () => {
      renameSync(join(ws.dir, "a"), join(ws.dir, "a.bak"));
      symlinkSync(ws.outside, join(ws.dir, "a"));
    };
    await assert.rejects(readSnapshot(root, "a/notes.md", undefined, { afterAncestors: swap }), { code: "unsafe_path" });
    // And a file that grows after the open is refused rather than read to EOF.
    renameSync(join(ws.dir, "a"), join(ws.dir, "a.link"));
    renameSync(join(ws.dir, "a.bak"), join(ws.dir, "a"));
    const grow = async () => writeFileSync(join(ws.dir, "a", "notes.md"), Buffer.concat([CONTENT, CONTENT]));
    await assert.rejects(readSnapshot(root, "a/notes.md", undefined, { afterOpen: grow }), { code: "unsafe_path" });
  } finally {
    ws.cleanup();
  }
});

for (const [status, pattern] of [
  [401, /reconnect this device/],
  [403, /artifacts:publish/],
  [404, /backend detail/],
  [409, /backend detail/],
  [413, /backend detail/],
  [422, /rejected the tool arguments: backend detail/],
  [500, /outcome unknown: NOUS request failed \(500\): backend detail.*same publication_id/],
  [503, /backend detail/],
] as const)
  test(`gateway ${status} during publication becomes a tool error, not a protocol error`, async () => {
    const ws = workspace();
    const server = createServer((_request, response) => {
      response.writeHead(status, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ detail: "backend detail" }));
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("no port");
    try {
      const http = new ArtifactHttpClient(`http://127.0.0.1:${address.port}/api/v1`, credentials);
      const publisher = createArtifactPublisher(http, await grantedRoot(ws.dir));
      const read = new CapabilityClient("http://127.0.0.1:1/api/v1", credentials, (async () => new Response("[]", { status: 200 })) as typeof fetch);
      const mcpServer = createNousMcpServer(read, [artifactsPublishTool(publisher)]);
      const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
      const mcp = new Client({ name: "test", version: "0" });
      await mcpServer.connect(serverTransport);
      await mcp.connect(clientTransport);
      const result = (await mcp.callTool({ name: "artifacts_publish", arguments: { relative_path: "report.md", title: "R", publication_id: randomUUID() } })) as { isError?: boolean; content: { text: string }[] };
      assert.equal(result.isError, true);
      assert.match(result.content[0]?.text ?? "", pattern);
      await mcp.close();
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
      ws.cleanup();
    }
  });

test("catalog tools that collide with a local tool name are dropped, never duplicated", async () => {
  const ws = workspace();
  const { client } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    const read = new CapabilityClient("http://127.0.0.1:1/api/v1", credentials, (async () =>
      new Response(JSON.stringify([{ name: "artifacts_publish", description: "impostor", input_schema: { type: "object" } }]), { status: 200 })) as typeof fetch);
    const server = createNousMcpServer(read, [artifactsPublishTool(publisher)]);
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const mcp = new Client({ name: "test", version: "0" });
    await server.connect(serverTransport);
    await mcp.connect(clientTransport);
    const listed = await mcp.listTools();
    assert.equal(listed.tools.length, 1);
    assert.notEqual(listed.tools[0]?.description, "impostor");
    await mcp.close();
  } finally {
    ws.cleanup();
  }
});

test("mcp install binds a root only when unambiguous or explicit", async () => {
  const ws = workspace();
  const stateDir = join(ws.outside, "state");
  const store = new CredentialStore(stateDir);
  const announced: string[] = [];
  const base: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    projectId: randomUUID(),
    credentialHandle: randomUUID(),
    scopes: ["harness:execute", "tools:read", "artifacts:publish"],
    workspaces: [],
  };
  const install = async (state: LocalState, root?: string) => {
    await store.writeLocal("connection", state);
    return mcpInstallCommand(stateDir, { root, announce: (m) => announced.push(m) });
  };
  try {
    assert.equal((await install(base)).includes("--root"), false);
    const one = { id: randomUUID(), root: ws.dir, label: "w", projectId: base.projectId };
    const command = await install({ ...base, workspaces: [one] });
    assert.ok(command.includes(`'--root' '${ws.dir}'`));
    assert.match(announced.at(-1) ?? "", new RegExp(ws.dir));
    const two = { ...one, id: randomUUID(), root: ws.outside };
    await assert.rejects(install({ ...base, workspaces: [one, two] }), /pass --root/);
    assert.ok((await install({ ...base, workspaces: [one, two] }, ws.outside)).includes(`'--root' '${ws.outside}'`));
    await assert.rejects(install({ ...base, workspaces: [one, two] }, "/nope"), /registered workspace root/);
    // Without the publish scope no root is ever bound.
    assert.equal((await install({ ...base, scopes: ["harness:execute", "tools:read"], workspaces: [one] })).includes("--root"), false);
  } finally {
    ws.cleanup();
  }
});


test("a changed file or title under the same publication_id is a conflict, not a duplicate", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    const publicationId = randomUUID();
    await publisher.publish({ relativePath: "report.md", title: "Report", publicationId });
    writeFileSync(join(ws.dir, "report.md"), "edited\n");
    await assert.rejects(publisher.publish({ relativePath: "report.md", title: "Report", publicationId }), /conflict/);
    assert.equal(calls.upload.length, 1);
    writeFileSync(join(ws.dir, "report.md"), CONTENT);
    await assert.rejects(publisher.publish({ relativePath: "report.md", title: "Renamed", publicationId }), /conflict/);
  } finally {
    ws.cleanup();
  }
});

test("artifact_id and expected_parent_version_id pass through exactly, or are refused", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    const read = new CapabilityClient("http://127.0.0.1:1/api/v1", credentials, (async () => new Response("[]", { status: 200 })) as typeof fetch);
    const server = createNousMcpServer(read, [artifactsPublishTool(publisher)]);
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const mcp = new Client({ name: "test", version: "0" });
    await server.connect(serverTransport);
    await mcp.connect(clientTransport);
    const artifactId = randomUUID(), parent = randomUUID();
    await mcp.callTool({ name: "artifacts_publish", arguments: { relative_path: "report.md", title: "R", publication_id: randomUUID(), artifact_id: artifactId, expected_parent_version_id: parent } });
    assert.equal(calls.finalize[0]?.artifact_id, artifactId);
    assert.equal(calls.finalize[0]?.expected_parent_version_id, parent);
    await mcp.callTool({ name: "artifacts_publish", arguments: { relative_path: "report.md", title: "R", publication_id: randomUUID() } });
    assert.equal("artifact_id" in (calls.finalize[1] ?? {}), false);
    assert.equal("expected_parent_version_id" in (calls.finalize[1] ?? {}), false);
    for (const args of [
      { relative_path: "report.md", title: "R", publication_id: randomUUID(), artifact_id: "nope" },
      { relative_path: "report.md", title: "   ", publication_id: randomUUID() },
      { relative_path: "  ", title: "R", publication_id: randomUUID() },
    ]) {
      const result = (await mcp.callTool({ name: "artifacts_publish", arguments: args })) as { isError?: boolean; content: { text: string }[] };
      assert.equal(result.isError, true);
      assert.match(result.content[0]?.text ?? "", /requires|must be UUIDs/);
    }
    assert.equal(calls.reserve.length, 2);
    await mcp.close();
  } finally {
    ws.cleanup();
  }
});

test("missing files and unreadable files are named honestly, not called unsafe", async () => {
  const ws = workspace();
  try {
    const root = await grantedRoot(ws.dir);
    await assert.rejects(readSnapshot(root, "missing.md"), { code: "not_found" });
    await assert.rejects(readSnapshot(root, "nope/missing.md"), { code: "not_found" });
    writeFileSync(join(ws.dir, "locked.md"), CONTENT);
    chmodSync(join(ws.dir, "locked.md"), 0o200);
    if (process.getuid?.() !== 0)
      await assert.rejects(readSnapshot(root, "locked.md"), (error: unknown) => (error as { code: string; message: string }).code === "unsafe_path" && /EACCES/.test((error as Error).message));
  } finally {
    ws.cleanup();
  }
});

test("MIME comes from the extension, case-insensitively, with octet-stream as the fallback", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    writeFileSync(join(ws.dir, "data.xyz"), CONTENT);
    writeFileSync(join(ws.dir, "REPORT.MD"), CONTENT);
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    await publisher.publish({ relativePath: "data.xyz", title: "d", publicationId: randomUUID() });
    await publisher.publish({ relativePath: "REPORT.MD", title: "r", publicationId: randomUUID() });
    assert.deepEqual(calls.reserve.map((r) => r.mime_type), ["application/octet-stream", "text/markdown"]);
  } finally {
    ws.cleanup();
  }
});

/** Real backend over HTTP for the spawned server: catalog + artifact routes. */
async function artifactBackend() {
  const requests: { method: string; path: string; headers: IncomingMessage["headers"]; body: Buffer }[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c) => chunks.push(c));
    request.on("end", () => {
      requests.push({ method: request.method ?? "", path: request.url ?? "", headers: request.headers, body: Buffer.concat(chunks) });
      response.writeHead(request.method === "PUT" ? 204 : 200, { "Content-Type": "application/json" });
      if (request.method === "PUT") return response.end();
      if (request.url?.endsWith("/integrations/tools")) return response.end(JSON.stringify([{ name: "search_documents", description: "d", input_schema: { type: "object" } }]));
      if (request.url?.endsWith("/tools/read")) return response.end(JSON.stringify({ content: [], is_error: false, source_refs: [] }));
      if (request.url?.endsWith("/artifacts/uploads")) return response.end(JSON.stringify({ upload_id: "22222222-2222-4222-8222-222222222222", expires_at: new Date().toISOString() }));
      return response.end(JSON.stringify(version("33333333-3333-4333-8333-333333333333")));
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  return { origin: `http://127.0.0.1:${address.port}/api/v1`, requests, close: () => new Promise<void>((resolve) => server.close(() => resolve())) };
}

/** Drive a spawned `nous-harness mcp` with raw JSON-RPC over stdio. */
async function driveStdio(command: string, args: string[], cwd: string, calls: { name: string; arguments: Record<string, unknown> }[]) {
  const child = spawn(command, args, { cwd, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, CODEX_HOME: cwd, HOME: cwd } });
  let stdout = "", stderr = "";
  child.stdout.on("data", (c) => (stdout += c));
  child.stderr.on("data", (c) => (stderr += c));
  const send = (m: object) => child.stdin.write(JSON.stringify(m) + "\n");
  const waitFor = (id: number) => new Promise<Record<string, any>>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no response ${id}\n${stderr}`)), 20_000);
    const check = () => {
      for (const line of stdout.split("\n").filter(Boolean)) {
        let parsed: Record<string, any>;
        try { parsed = JSON.parse(line); } catch { clearTimeout(timer); child.stdout.off("data", check); return reject(new Error(`non JSON-RPC stdout: ${line}`)); }
        if (parsed.id === id) { clearTimeout(timer); child.stdout.off("data", check); resolve(parsed); }
      }
    };
    child.stdout.on("data", check);
    check();
  });
  send({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "0" } } });
  await waitFor(1);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });
  send({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  const tools = (await waitFor(2)).result.tools as { name: string; description: string }[];
  const results: Record<string, any>[] = [];
  let id = 3;
  for (const call of calls) {
    send({ jsonrpc: "2.0", id, method: "tools/call", params: call });
    results.push((await waitFor(id)).result);
    id += 1;
  }
  child.stdin.end();
  const [code] = (await once(child, "exit")) as [number | null];
  return { tools, results, stderr, code };
}

test("the spawned MCP child publishes through the managed argv and degrades when the root is gone", async () => {
  const ws = workspace();
  const backend = await artifactBackend();
  const stateDir = join(ws.outside, "state");
  const codexHome = join(ws.outside, "home");
  mkdirSync(codexHome);
  const store = new CredentialStore(stateDir);
  const credentialHandle = await store.save({ accessToken: "cli-jwt", grantToken: credentials.grantToken });
  const binding = { id: randomUUID(), root: ws.dir, label: "w", projectId: randomUUID() };
  const state: LocalState = {
    apiUrl: backend.origin,
    deviceId: randomUUID(),
    projectId: binding.projectId,
    credentialHandle,
    scopes: ["harness:execute", "tools:read", "artifacts:publish"],
    workspaces: [binding],
  };
  try {
    const managed = sessionOptionsFor(stateDir, state, binding.id).mcpConfig!.nous!;
    const publicationId = randomUUID();
    const run = await driveStdio(managed.command, managed.args, codexHome, [
      { name: "artifacts_publish", arguments: { relative_path: "report.md", title: "Report", publication_id: publicationId } },
      { name: "search_documents", arguments: { query: "x" } },
    ]);
    assert.equal(run.code, 0, run.stderr);
    assert.deepEqual(run.tools.map((t) => t.name).sort(), ["artifacts_publish", "search_documents"]);
    assert.equal(run.results[0]?.isError, undefined, JSON.stringify(run.results[0]));
    assert.equal(run.results[0]?.structuredContent?.version_id, "33333333-3333-4333-8333-333333333333");
    const paths = backend.requests.map((r) => `${r.method} ${r.path.replace(/^\/api\/v1/, "")}`);
    assert.deepEqual(paths.filter((p) => p.includes("artifacts")), ["POST /artifacts/uploads", "PUT /artifacts/uploads/22222222-2222-4222-8222-222222222222/content", "POST /artifacts/versions"]);
    const put = backend.requests.find((r) => r.method === "PUT")!;
    assert.equal(createHash("sha256").update(put.body).digest("hex"), DIGEST);
    for (const request of backend.requests) assert.equal(request.headers["x-nous-integration-grant"], credentials.grantToken);
    assert.equal(JSON.parse(backend.requests.find((r) => r.path.endsWith("/uploads"))!.body.toString()).publication_id, publicationId);

    // Root removed after registration: read tools stay, publish explains itself.
    const gone = buildManagedMcpConfig({ apiOrigin: backend.origin, credentialHandle, stateDir, outputRoot: join(ws.outside, "vanished") }).nous!;
    const degraded = await driveStdio(gone.command, gone.args, codexHome, [
      { name: "artifacts_publish", arguments: { relative_path: "report.md", title: "R", publication_id: randomUUID() } },
      { name: "search_documents", arguments: { query: "x" } },
    ]);
    assert.equal(degraded.code, 0, degraded.stderr);
    assert.deepEqual(degraded.tools.map((t) => t.name).sort(), ["artifacts_publish", "search_documents"]);
    assert.match(degraded.tools.find((t) => t.name === "artifacts_publish")?.description ?? "", /Unavailable/);
    assert.equal(degraded.results[0]?.isError, true);
    assert.match(degraded.results[0]?.content?.[0]?.text ?? "", /unavailable/);
    assert.equal(degraded.results[1]?.isError, false);
    assert.match(degraded.stderr, /artifacts_publish unavailable/);
  } finally {
    await backend.close();
    ws.cleanup();
  }
});

test("mcp install prints one stdout line, announces the root on stderr, and accepts a symlinked --root", async () => {
  const ws = workspace();
  const stateDir = join(ws.outside, "state");
  const store = new CredentialStore(stateDir);
  const link = join(ws.outside, "root-link");
  symlinkSync(ws.dir, link);
  try {
    await store.writeLocal("connection", {
      apiUrl: "https://nous.example/api/v1",
      deviceId: randomUUID(),
      projectId: randomUUID(),
      credentialHandle: randomUUID(),
      scopes: ["harness:execute", "tools:read", "artifacts:publish"],
      workspaces: [{ id: randomUUID(), root: ws.dir, label: "w", projectId: randomUUID() }, { id: randomUUID(), root: ws.outside, label: "o", projectId: randomUUID() }],
    } satisfies LocalState);
    const cliPath = fileURLToPath(new URL("../src/cli.ts", import.meta.url));
    const child = spawn(process.execPath, ["--import", fileURLToPath(import.meta.resolve("tsx")), cliPath, "mcp", "install", "--store", stateDir, "--root", link], { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const [code] = (await once(child, "exit")) as [number | null];
    assert.equal(code, 0, stderr);
    assert.equal(stdout.trim().split("\n").length, 1);
    assert.ok(stdout.includes(`'--root' '${ws.dir}'`), stdout);
    assert.match(stderr, /will publish from/);
    // --root without the publish scope is refused rather than ignored.
    await store.writeLocal("connection", { apiUrl: "https://nous.example/api/v1", deviceId: randomUUID(), projectId: randomUUID(), credentialHandle: randomUUID(), scopes: ["harness:execute", "tools:read"], workspaces: [] } satisfies LocalState);
    await assert.rejects(mcpInstallCommand(stateDir, { root: ws.dir, announce: () => {} }), /--root requires artifacts:publish/);
  } finally {
    ws.cleanup();
  }
});


test("an empty file is refused locally before any reservation", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    writeFileSync(join(ws.dir, "empty.md"), "");
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    await assert.rejects(publisher.publish({ relativePath: "empty.md", title: "e", publicationId: randomUUID() }), { code: "empty" });
    assert.equal(calls.reserve.length, 0);
  } finally {
    ws.cleanup();
  }
});

test("large files are read completely even when the OS returns short reads", async () => {
  const ws = workspace();
  try {
    const big = Buffer.alloc(3 * 1024 * 1024, 7);
    writeFileSync(join(ws.dir, "big3.bin"), big);
    const bytes = await readSnapshot(await grantedRoot(ws.dir), "big3.bin");
    assert.equal(bytes.byteLength, big.length);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), createHash("sha256").update(big).digest("hex"));
  } finally {
    ws.cleanup();
  }
});
