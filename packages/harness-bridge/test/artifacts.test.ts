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
import { CapabilityClient } from "../src/mcp/client.ts";
import { createNousMcpServer } from "../src/mcp/server.ts";
import { sessionOptionsFor, type LocalState } from "../src/cli.ts";

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
  const client: ArtifactApiClient = {
    async reserve(request) {
      calls.reserve.push(request);
      return { upload_id: "11111111-1111-4111-8111-" + request.publication_id.slice(-12), expires_at: new Date().toISOString() } as ArtifactUploadDTO;
    },
    async upload(id, bytes) {
      calls.upload.push(id + ":" + createHash("sha256").update(bytes).digest("hex"));
    },
    async finalize(request) {
      calls.finalize.push(request);
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
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "nous-artifacts-")));
  const outside = realpathSync(mkdtempSync(join(tmpdir(), "nous-outside-")));
  writeFileSync(join(dir, "report.md"), CONTENT);
  mkdirSync(join(dir, "nested"));
  writeFileSync(join(dir, "nested", "chart.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  writeFileSync(join(outside, "secret.txt"), "secret\n");
  symlinkSync(join(outside, "secret.txt"), join(dir, "link.md"));
  symlinkSync(outside, join(dir, "linkdir"));
  writeFileSync(join(dir, "hard.md"), CONTENT);
  linkSync(join(dir, "hard.md"), join(outside, "hard-alias.md"));
  writeFileSync(join(dir, "big.bin"), Buffer.alloc(MAX_SNAPSHOT_BYTES + 1));
  return { dir, outside, cleanup: () => { rmSync(dir, { recursive: true, force: true }); rmSync(outside, { recursive: true, force: true }); } };
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
    await assert.rejects(publisher.publish({ relativePath: "missing.md", title: "x", publicationId: randomUUID() }), { code: "unsafe_path" });
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

test("publish sequences reserve, upload and finalize; a retry keeps the version", async () => {
  const ws = workspace();
  const { client, calls } = fakeClient();
  try {
    const publisher = createArtifactPublisher(client, await grantedRoot(ws.dir));
    const publicationId = randomUUID();
    const first = await publisher.publish({ relativePath: "report.md", title: "Report", publicationId });
    const second = await publisher.publish({ relativePath: "report.md", title: "Report", publicationId });
    assert.equal(first.version_id, second.version_id);
    assert.deepEqual(calls.reserve[0], { publication_id: publicationId, byte_size: CONTENT.length, mime_type: "text/markdown", sha256: DIGEST });
    assert.equal(calls.upload[0], calls.reserve[0] && "11111111-1111-4111-8111-" + publicationId.slice(-12) + ":" + DIGEST);
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
      if (status !== 200) return response.end(JSON.stringify({ detail: "Artifact publication is disabled" }));
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
    await assert.rejects(client.reserve({ publication_id: randomUUID(), byte_size: 7, mime_type: "text/markdown", sha256: DIGEST }), /Artifact publication is disabled/);
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
