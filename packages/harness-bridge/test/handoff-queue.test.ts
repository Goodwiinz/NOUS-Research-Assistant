import assert from "node:assert/strict";
import { test } from "node:test";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import { once } from "node:events";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { handoffDiscard, handoffFlush, handoffList, handoffSave, handoffShow, type LocalState } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";
import { GrantExpired } from "../src/grants.ts";
import { HandoffHttpClient, type Handoff } from "../src/handoffs/client.ts";
import { saveHandoffTool } from "../src/handoffs/mcp.ts";
import { HandoffQueue, localBinding, type QueueEntry } from "../src/handoffs/queue.ts";
import { ToolRequestRejected } from "../src/mcp/client.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const THREAD = "99999999-9999-4999-8999-999999999999";
const OTHER_THREAD = "88888888-8888-4888-8888-888888888888";

type Mode = "offline" | "ok" | "conflict" | "forbidden" | "invalid" | "unauthorized" | "busy" | "error";

function dto(version: number, handoffId: string = randomUUID()): Handoff {
  return {
    id: randomUUID(),
    thread_id: THREAD,
    project_id: PROJECT,
    version,
    handoff_id: handoffId,
    goal: "Finish the review",
    decisions: [],
    remaining: [],
    results: [],
    harness_name: "codex",
    harness_session_id: null,
    created_at: "2026-10-05T00:00:00Z",
  };
}

/** Fake NOUS counting POSTs per handoff_id; `observe` runs before the reply. */
async function backend(observe: (handoffId: string) => void = () => {}) {
  const posts = new Map<string, number>();
  const state = { mode: "ok" as Mode, latest: null as Handoff | null };
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (c) => chunks.push(c));
    request.on("end", () => {
      if (request.method === "GET") {
        if (state.mode === "offline") return request.socket.destroy();
        const body = state.latest;
        response.writeHead(body ? 200 : 404, { "Content-Type": "application/json" });
        return response.end(JSON.stringify(body ?? { detail: "none" }));
      }
      const id = JSON.parse(Buffer.concat(chunks).toString()).handoff_id as string;
      posts.set(id, (posts.get(id) ?? 0) + 1);
      observe(id);
      if (state.mode === "offline") return request.socket.destroy();
      const [code, body]: [number, unknown] = {
        ok: [201, dto(1, id)],
        conflict: [409, { detail: "expected_parent_version is stale", latest: dto(7) }],
        forbidden: [403, { detail: "no" }],
        invalid: [422, { detail: "bad goal" }],
        unauthorized: [401, { detail: "expired" }],
        busy: [429, { detail: "slow down" }],
        error: [500, { detail: "boom" }],
      }[state.mode as Exclude<Mode, "offline">] as [number, unknown];
      response.writeHead(code, { "Content-Type": "application/json" });
      response.end(JSON.stringify(body));
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  return {
    posts,
    state,
    origin: `http://127.0.0.1:${address.port}/api/v1`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

/** A connected, chat-bound device whose grant is fresh (no renewal call). */
async function connected(origin: string, threadId = THREAD) {
  const dir = mkdtempSync(join(tmpdir(), "nous-queue-"));
  const store = new CredentialStore(dir);
  const credentialHandle = await store.save({
    accessToken: "cli-secret",
    grantToken: "grant-secret",
    grantId: "33333333-3333-4333-8333-333333333333",
    renewedAt: Date.now(),
  });
  await store.writeLocal("connection", {
    apiUrl: origin,
    deviceId: "22222222-2222-4222-8222-222222222222",
    projectId: PROJECT,
    threadId,
    credentialHandle,
    scopes: ["harness:execute", "tools:read", "handoff:read", "handoff:write"],
    workspaces: [],
  } satisfies LocalState);
  const lines: string[] = [];
  const file = join(dir, "handoff.json");
  writeFileSync(file, JSON.stringify({ goal: "Finish the review", decisions: ["PRISMA"] }));
  return {
    dir,
    store,
    file,
    lines,
    options: { stateDir: dir, announce: (m: string) => lines.push(m) },
    entries: async () => new HandoffQueue(store, { save: async () => { throw new Error("unused"); } }).list(),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

test("an offline save stays pending, one flush delivers it, and a second flush is a no-op", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    nous.state.mode = "offline";
    const attempt = await handoffSave({ ...t.options, file: t.file });
    assert.equal(attempt.state, "pending");
    const [entry] = await t.entries();
    assert.equal(entry.state, "pending");
    assert.equal(entry.thread_id, THREAD);
    assert.equal(entry.project_id, PROJECT);
    assert.equal(entry.body.expected_parent_version, null);
    assert.equal(entry.body.goal, "Finish the review");
    assert.ok(entry.last_error);

    nous.state.mode = "ok";
    const flushed = await handoffFlush(t.options);
    assert.deepEqual(flushed.attempted.map((a) => a.attempt.state), ["done"]);
    assert.deepEqual(await t.entries(), []);
    // The retry reused the journaled handoff_id, so NOUS can dedupe it.
    assert.deepEqual([...nous.posts.entries()], [[entry.handoff_id, 2]]);

    const again = await handoffFlush(t.options);
    assert.equal(again.attempted.length, 0);
    assert.equal(nous.posts.get(entry.handoff_id), 2);
    assert.match(t.lines.at(-1)!, /no pending handoffs/i);
    assert.ok(!t.lines.join("\n").includes("secret"));
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("--parent sets expected_parent_version and a 2xx save leaves no entry", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    const attempt = await handoffSave({ ...t.options, file: t.file, parent: "3" });
    assert.equal(attempt.state, "done");
    assert.deepEqual(await t.entries(), []);
    await assert.rejects(handoffSave({ ...t.options, file: t.file, parent: "0" }), /--parent/);
    assert.equal(nous.posts.size, 1);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("a 409 marks the entry conflicted, stores and prints the latest, and flush leaves it alone", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    nous.state.mode = "conflict";
    const attempt = await handoffSave({ ...t.options, file: t.file, parent: "2" });
    assert.equal(attempt.state, "conflicted");
    const [entry] = await t.entries();
    assert.equal(entry.state, "conflicted");
    assert.equal(entry.latest?.version, 7);
    assert.equal(entry.last_error, "expected_parent_version is stale");
    assert.match(t.lines.join("\n"), /latest handoff is version 7/);
    assert.match(t.lines.join("\n"), /"version": 7/);
    nous.state.mode = "ok";
    const flushed = await handoffFlush(t.options);
    assert.equal(flushed.attempted.length, 0);
    assert.equal(nous.posts.get(entry.handoff_id), 1);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("403 and 422 are terminal: the entry is kept as rejected with the error", async () => {
  for (const mode of ["forbidden", "invalid"] as const) {
    const nous = await backend();
    const t = await connected(nous.origin);
    try {
      nous.state.mode = mode;
      const attempt = await handoffSave({ ...t.options, file: t.file });
      assert.equal(attempt.state, "rejected");
      const [entry] = await t.entries();
      assert.equal(entry.state, "rejected");
      assert.match(entry.last_error!, mode === "forbidden" ? /handoff:write/ : /bad goal/);
      nous.state.mode = "ok";
      assert.equal((await handoffFlush(t.options)).attempted.length, 0);
      assert.equal(nous.posts.get(entry.handoff_id), 1);
    } finally {
      await nous.close();
      t.cleanup();
    }
  }
});

test("401, 429, 5xx and an expired grant stay pending; auth failures print a reconnect hint", async () => {
  for (const mode of ["unauthorized", "busy", "error"] as const) {
    const nous = await backend();
    const t = await connected(nous.origin);
    try {
      nous.state.mode = mode;
      const attempt = await handoffSave({ ...t.options, file: t.file });
      assert.equal(attempt.state, "pending", mode);
      assert.equal((await t.entries())[0].state, "pending");
      assert.equal(/nous-harness connect/.test(t.lines.join("\n")), mode === "unauthorized", mode);
    } finally {
      await nous.close();
      t.cleanup();
    }
  }
  const dir = mkdtempSync(join(tmpdir(), "nous-queue-expired-"));
  try {
    const queue = new HandoffQueue(new CredentialStore(dir), { save: async () => { throw new GrantExpired(); } });
    const attempt = await queue.submit({ threadId: THREAD, projectId: PROJECT }, {
      handoff_id: randomUUID(),
      expected_parent_version: null,
      goal: "g",
      harness_name: "x",
    });
    assert.equal(attempt.state, "pending");
    assert.equal(attempt.state === "pending" && attempt.reconnect, true);
    assert.equal((await queue.list())[0].state, "pending");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("flush skips pending entries journaled for another chat", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    nous.state.mode = "offline";
    const stale = new HandoffQueue(t.store, new HandoffHttpClient(nous.origin, { accessToken: "a", grantToken: "b" }));
    await stale.submit({ threadId: OTHER_THREAD, projectId: PROJECT }, {
      handoff_id: "44444444-4444-4444-8444-444444444444",
      expected_parent_version: null,
      goal: "old chat",
      harness_name: "x",
    });
    await handoffSave({ ...t.options, file: t.file });
    nous.state.mode = "ok";
    const flushed = await handoffFlush(t.options);
    assert.equal(flushed.attempted.length, 1);
    assert.deepEqual(flushed.skipped.map((e) => e.handoff_id), ["44444444-4444-4444-8444-444444444444"]);
    assert.equal(nous.posts.get("44444444-4444-4444-8444-444444444444"), 1);
    assert.match(t.lines.join("\n"), /Skipped 44444444-4444-4444-8444-444444444444/);
    const left = await t.entries();
    assert.deepEqual(left.map((e: QueueEntry) => [e.thread_id, e.state]), [[OTHER_THREAD, "pending"]]);
    await handoffList(t.options);
    assert.match(t.lines.at(-1)!, /44444444-4444-4444-8444-444444444444\s+pending/);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("save_nous_handoff journals before the POST and removes the entry after a 2xx", async () => {
  let dir = "";
  const seenJournal: boolean[] = [];
  const nous = await backend((id) => seenJournal.push(existsSync(join(dir, `handoff-${id}.json`))));
  const t = await connected(nous.origin);
  dir = t.dir;
  try {
    const client = new HandoffHttpClient(nous.origin, { accessToken: "a", grantToken: "b" });
    const queue = new HandoffQueue(t.store, client);
    const tool = saveHandoffTool(queue, async () => ({ threadId: THREAD, projectId: PROJECT }));
    const handoffId = randomUUID();
    const outcome = await tool.call({ handoff_id: handoffId, expected_parent_version: null, goal: "g" });
    assert.match(outcome.text, /^Saved handoff version 1/);
    assert.deepEqual(seenJournal, [true]);
    assert.equal(existsSync(join(t.dir, `handoff-${handoffId}.json`)), false);

    // Offline: the tool reports a queued pending entry instead of losing it.
    nous.state.mode = "offline";
    const queued = await tool.call({ handoff_id: handoffId, expected_parent_version: null, goal: "g" });
    assert.equal(queued.isError, true);
    assert.match(queued.text, /queued locally/);
    assert.match(queued.text, new RegExp(`handoff_id ${handoffId}`));
    assert.equal((await queue.list())[0].state, "pending");

    // Pending outcomes (5xx, 401) report the queued handoff_id instead of throwing.
    for (const mode of ["error", "unauthorized"] as const) {
      nous.state.mode = mode;
      const id = randomUUID();
      const pending = await tool.call({ handoff_id: id, expected_parent_version: null, goal: "g" });
      assert.equal(pending.isError, true, mode);
      assert.match(pending.text, new RegExp(`queued locally as pending under handoff_id ${id}`), mode);
      assert.equal(/nous-harness connect/.test(pending.text), mode === "unauthorized", mode);
    }
    // A terminal rejection keeps its MCP error class.
    nous.state.mode = "forbidden";
    await assert.rejects(tool.call({ expected_parent_version: null, goal: "g" }), ToolRequestRejected);

    // No chat binding in local state: refuse before journaling anything.
    const unbound = saveHandoffTool(queue, async () => null);
    const before = nous.posts.size;
    const refused = await unbound.call({ expected_parent_version: null, goal: "g" });
    assert.equal(refused.isError, true);
    assert.match(refused.text, /--chat/);
    assert.equal(nous.posts.size, before);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("handoff show prints the latest handoff or says there is none", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    assert.equal(await handoffShow(t.options), null);
    assert.match(t.lines.at(-1)!, /no handoff yet/i);
    nous.state.latest = dto(3);
    assert.equal((await handoffShow(t.options))?.version, 3);
    assert.match(t.lines.at(-1)!, /"version": 3/);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("handoff commands refuse a connection without the handoff scopes or a chat", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    const state = (await t.store.readLocal("connection")) as LocalState;
    await t.store.writeLocal("connection", { ...state, scopes: ["harness:execute", "tools:read"] });
    await assert.rejects(handoffSave({ ...t.options, file: t.file }), /--handoff/);
    await t.store.writeLocal("connection", { ...state, threadId: undefined });
    await assert.rejects(handoffFlush(t.options), /--chat/);
    assert.equal(nous.posts.size, 0);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("a stale MCP session handle never journals under the device's new binding", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    const state = (await t.store.readLocal("connection")) as LocalState;
    assert.deepEqual(await localBinding(t.store, state.credentialHandle), { threadId: THREAD, projectId: PROJECT });
    // The device reconnected to another chat under a new handle; this child still holds the old one.
    const staleHandle = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    assert.equal(await localBinding(t.store, staleHandle), null);
    const client = new HandoffHttpClient(nous.origin, { accessToken: "a", grantToken: "b" });
    const tool = saveHandoffTool(new HandoffQueue(t.store, client), () => localBinding(t.store, staleHandle));
    const refused = await tool.call({ expected_parent_version: null, goal: "g" });
    assert.equal(refused.isError, true);
    assert.match(refused.text, /Reconnect this MCP session/);
    assert.deepEqual(await t.entries(), []);
    assert.equal(nous.posts.size, 0);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("handoff discard drops one entry in any state and reports it", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    nous.state.mode = "conflict";
    await handoffSave({ ...t.options, file: t.file, parent: "2" });
    const [entry] = await t.entries();
    const dropped = await handoffDiscard({ ...t.options, handoffId: entry.handoff_id });
    assert.equal(dropped.state, "conflicted");
    assert.match(t.lines.at(-1)!, new RegExp(`Discarded ${entry.handoff_id} \\(conflicted`));
    assert.deepEqual(await t.entries(), []);
    await assert.rejects(handoffDiscard({ ...t.options, handoffId: entry.handoff_id }), /no queued handoff/);
    await assert.rejects(handoffDiscard({ ...t.options, handoffId: "nope" }), /UUID/);
  } finally {
    await nous.close();
    t.cleanup();
  }
});

test("missing local credentials give the reconnect hint, not a store error", async () => {
  const nous = await backend();
  const t = await connected(nous.origin);
  try {
    const state = (await t.store.readLocal("connection")) as LocalState;
    await t.store.removeLocal(state.credentialHandle);
    await assert.rejects(handoffShow(t.options), /local credentials are missing.*nous-harness connect --chat UUID --tools --handoff/);
    await assert.rejects(handoffSave({ ...t.options, file: t.file }), /nous-harness connect --chat/);
    assert.deepEqual(await t.entries(), []);
  } finally {
    await nous.close();
    t.cleanup();
  }
});
