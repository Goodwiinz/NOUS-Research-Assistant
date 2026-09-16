import assert from "node:assert/strict";
import { afterEach, beforeEach, mock, test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import React from "react";
import { render, cleanup } from "ink-testing-library";
import { App } from "./app";
import { saveConfig, loadConfig } from "../../frontend/cli/auth/store";
import type { BranchHistory, TerminalMessage } from "./services";
import {
  loadBranches,
  saveBranches,
  withoutThread,
  forgetBranches,
  forkThread,
} from "./services";
import type { useTerminalSession } from "./session";
let dir: string;
const originalConfigDir = process.env.NOUS_CONFIG_DIR;
const originalApi = process.env.NOUS_API_URL;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "nous-audit-"));
  process.env.NOUS_CONFIG_DIR = dir;
  process.env.NOUS_API_URL = "https://nous.invalid/api/v1";
  saveConfig({
    token: "fake",
    user_email: "fake@example.invalid",
    organization_id: "org",
    expires_at: "2099-01-01",
    thread_id: "thread-1",
  });
});
afterEach(() => {
  cleanup();
  mock.restoreAll();
  if (originalConfigDir === undefined) delete process.env.NOUS_CONFIG_DIR;
  else process.env.NOUS_CONFIG_DIR = originalConfigDir;
  if (originalApi === undefined) delete process.env.NOUS_API_URL;
  else process.env.NOUS_API_URL = originalApi;
  rmSync(dir, { force: true, recursive: true });
});
const response = (events: [string, unknown][]) =>
  new Response(
    events
      .map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`)
      .join(""),
    { headers: { "Content-Type": "text/event-stream" } },
  );
const initial: TerminalMessage[] = [
  {
    runtimeId: "u1",
    threadId: "thread-1",
    role: "user",
    content: "Original question",
    timestamp: 1,
  },
  {
    runtimeId: "a1",
    threadId: "thread-1",
    role: "assistant",
    content: "Original answer",
    timestamp: 2,
  },
];
async function until(fn: () => boolean) {
  for (let i = 0; i < 100 && !fn(); i++) await delay(20);
  assert.ok(fn(), "UI ready");
}
async function key(ui: ReturnType<typeof render>, text: string) {
  ui.stdin.write(text);
  await delay(70);
}
async function mount(props: React.ComponentProps<typeof App> = {}) {
  const ui = render(<App {...props} />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
  );
  await delay(70);
  return ui;
}
test("edit from help panel", async () => {
  const ui = await mount({ initialMessages: initial });
  await key(ui, "/help");
  await key(ui, "\r");
  assert.match(ui.lastFrame()!, /Edits and retries create/);
  await key(ui, "/edit 1");
  await key(ui, "\r");
  assert.match(
    ui.lastFrame()!,
    /Edit message/,
    "Edit must reveal the editor from non-chat panels",
  );
});
test("text panel after diff", async () => {
  const { writeFileSync } = await import("node:fs");
  const old = join(dir, "old.txt"),
    updated = join(dir, "new.txt");
  writeFileSync(old, "old content");
  writeFileSync(updated, "new content");
  const ui = await mount({ initialMessages: initial });
  await key(ui, `/diff ${old} | ${updated}`);
  await key(ui, "\r");
  await key(ui, "/history");
  await key(ui, "\r");
  assert.match(
    ui.lastFrame()!,
    /Original answer/,
    "History must replace a previous diff",
  );
});
test("deleted branches cannot be selected", async () => {
  const calls: { url: string; method?: string; body?: unknown }[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method });
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      return Response.json([]);
    },
  );
  const alternative: TerminalMessage[] = [
    {
      runtimeId: "u2",
      threadId: "branch-2",
      role: "user",
      content: "Edited question",
      timestamp: 3,
    },
    {
      runtimeId: "a2",
      threadId: "branch-2",
      role: "assistant",
      content: "Alternate answer",
      timestamp: 4,
    },
  ];
  const history: BranchHistory = {
    headId: "a1",
    nodes: [
      { message: initial[0], parentId: null },
      { message: initial[1], parentId: "u1" },
      { message: alternative[0], parentId: null },
      { message: alternative[1], parentId: "u2" },
    ],
  };
  const ui = await mount({ initialHistory: history });
  await key(ui, "/delete branch-2");
  await key(ui, "\r");
  await key(ui, "delete");
  await key(ui, "\r");
  await until(() => calls.some((c) => c.method === "DELETE"));
  await key(ui, "/branch 1 next");
  await key(ui, "\r");
  assert.notEqual(
    loadConfig()?.thread_id,
    "branch-2",
    "Deleted branch must no longer be selectable",
  );
});
test("cancelled forks stop further writes and stay busy until settled", async () => {
  const { useTerminalSession } = await import("./session");
  let session!: ReturnType<typeof useTerminalSession>;
  let release!: () => void;
  const writes: { url: string; body: unknown }[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      if (String(url).includes("/threads/thread-1?"))
        return Response.json({ conversation_id: "c1", title: "Original" });
      writes.push({ url: String(url), body });
      if (String(url).endsWith("/api/v2/threads")) {
        await new Promise<void>((r) => (release = r));
        return Response.json({ id: "orphan-branch" });
      }
      return Response.json({ id: "copied-message" });
    },
  );
  const messages: TerminalMessage[] = [
    ...initial,
    {
      runtimeId: "u3",
      threadId: "thread-1",
      role: "user",
      content: "Third question",
      timestamp: 3,
    },
    {
      runtimeId: "a3",
      threadId: "thread-1",
      role: "assistant",
      content: "Third answer",
      timestamp: 4,
    },
  ];
  function Harness() {
    session = useTerminalSession(messages);
    return null;
  }
  const ui = render(<Harness />);
  await until(() => !!session);
  const result = session.branch("u3", "Revised third question").catch((e) => e);
  await until(() => !!release);
  session.onCancel();
  await delay(30);
  assert.equal(
    session.isLoading,
    true,
    "Cancellation stays busy until the submitted write settles",
  );
  const before = writes.length;
  release();
  const error = await result;
  await delay(50);
  assert.match(String(error), /Branch orphan-branch could not be completed/);
  assert.equal(
    writes.length,
    before,
    "Cancelling must prevent additional prefix writes after the in-flight request settles",
  );
  ui.unmount();
});
test("chat input after cancelling a pending fork", async () => {
  let release!: () => void;
  const calls: { url: string; method?: string; body?: unknown }[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(url),
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      if (String(url).includes("/threads/thread-1?"))
        return Response.json({ conversation_id: "c1", title: "Original" });
      if (String(url).endsWith("/api/v2/threads")) {
        await new Promise<void>((r) => (release = r));
        return Response.json({ id: "orphan" });
      }
      if (String(url).endsWith("/agent/stream"))
        return response([
          ["token", { content: "Got the message" }],
          ["done", {}],
        ]);
      return Response.json({ id: "copied" });
    },
  );
  const ui = await mount({ initialMessages: initial });
  await key(ui, "/retry");
  await key(ui, "\r");
  await until(() => !!release);
  await key(ui, "\x03");
  assert.doesNotMatch(
    ui.lastFrame()!,
    /Ask NOUS/,
    "Composer stays disabled while the fork settles",
  );
  release();
  await until(() => !!ui.lastFrame()?.includes("Ask NOUS"));
  await delay(100);
  await key(ui, "keep this message");
  await key(ui, "\r");
  await until(() => calls.some((c) => c.url.endsWith("/agent/stream")));
  assert.ok(JSON.stringify(calls.at(-1)?.body).includes("keep this message"));
});

test("deleting a branch preserves shared prefixes and invalidates saved sibling graphs", async () => {
  const original: TerminalMessage = { ...initial[0], threadId: "deleted" };
  const child: TerminalMessage = { ...initial[1], threadId: "surviving" };
  const other: TerminalMessage = {
    ...initial[0],
    runtimeId: "other",
    threadId: "deleted",
  };
  const history: BranchHistory = {
    headId: "a1",
    nodes: [
      { message: original, parentId: null },
      { message: child, parentId: "u1" },
      { message: other, parentId: null },
    ],
  };
  await saveBranches(history);
  await forgetBranches("deleted", history);
  const next = withoutThread(history, "deleted");
  assert.deepEqual(
    await loadBranches("surviving"),
    JSON.parse(JSON.stringify(next)),
  );
  assert.equal(await loadBranches("deleted"), undefined);
  assert.deepEqual(
    next.nodes.map((n) => n.message.runtimeId),
    ["u1", "a1"],
  );
  assert.equal(next.nodes[0].message.threadId, undefined);
  assert.equal(next.nodes[1].parentId, "u1");
});

for (const title of ["x".repeat(500), "🧪".repeat(500)]) {
  test(`fork titles fit the backend limit (${[...title][0]})`, async () => {
    let createdTitle = "";
    mock.method(
      globalThis,
      "fetch",
      async (_url: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          createdTitle = JSON.parse(String(init.body)).title;
          return Response.json({ id: "branch" });
        }
        return Response.json({ conversation_id: "conversation", title });
      },
    );
    await forkThread("thread-1", []);
    assert.ok(
      [...createdTitle].length <= 500,
      "Branch title exceeds the backend limit",
    );
    assert.ok(createdTitle.endsWith(" · branch"));
    assert.doesNotMatch(
      createdTitle,
      /[\uD800-\uDFFF]/u,
      "Title must not split a Unicode character",
    );
  });
}

test("forked assistant messages retain citations on the server", async () => {
  const citations = [
    { document_id: "paper-1", document_title: "Evidence", page_number: 3 },
  ];
  const copied: Record<string, unknown>[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "POST")
        return Response.json({
          conversation_id: "conversation",
          title: "Source",
        });
      if (String(url).endsWith("/messages"))
        copied.push(JSON.parse(String(init.body)));
      return Response.json({ id: "branch" });
    },
  );
  await forkThread("thread-1", [{ ...initial[1], contexts: citations }]);
  assert.deepEqual(copied[0].citations, citations);
});

for (const command of ["like", "dislike"]) {
  test(`${command} refuses a persisted user message`, async () => {
    const writes: string[] = [];
    mock.method(globalThis, "fetch", async (url: RequestInfo | URL) => {
      writes.push(String(url));
      return Response.json({});
    });
    const ui = await mount({
      initialMessages: initial.map((m) => ({
        ...m,
        serverId: `server-${m.runtimeId}`,
      })),
    });
    await key(ui, `/${command} 1`);
    await key(ui, "\r");
    assert.deepEqual(writes, [], "Feedback must not rate a user's own prompt");
    assert.match(ui.lastFrame()!, /assistant/i);
  });
}

test("Ctrl+C parks queued prompts until an explicit send", async () => {
  let first!: ReadableStreamDefaultController<Uint8Array>;
  const prompts: string[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      prompts.push(JSON.parse(String(init?.body)).messages.at(-1).content);
      if (prompts.length > 1)
        return response([
          ["token", { content: "Resumed answer" }],
          ["done", {}],
        ]);
      return new Response(
        new ReadableStream({
          start(controller) {
            first = controller;
          },
        }),
      );
    },
  );
  const ui = await mount();
  await key(ui, "first");
  await key(ui, "\r");
  await until(() => prompts.length === 1);
  await key(ui, "parked");
  await key(ui, "\r");
  assert.match(ui.lastFrame()!, /Queued: parked/);
  await key(ui, "\x03");
  first.close();
  await until(() => !ui.lastFrame()?.includes("Working…"));
  await delay(100);
  assert.deepEqual(
    prompts,
    ["first"],
    "Stopping must not dispatch a queued follow-up",
  );
  assert.match(ui.lastFrame()!, /Queued: parked/);
  await key(ui, "resume");
  await key(ui, "\r");
  await until(() => prompts.length === 3);
  assert.deepEqual(prompts, ["first", "parked", "resume"]);
});

test("stream events do not reread config or recreate the user node", async () => {
  const fs = await import("node:fs");
  const { useTerminalSession } = await import("./session");
  let session!: ReturnType<typeof useTerminalSession>;
  let body!: ReadableStreamDefaultController<Uint8Array>;
  mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            body = controller;
          },
        }),
      ),
  );
  function Probe() {
    session = useTerminalSession([]);
    return null;
  }
  render(<Probe />);
  await until(() => !!session);
  const sending = session.onSend("stream a response");
  await until(() => !!body);
  const emit = (event: string, data: unknown) =>
    body.enqueue(
      new TextEncoder().encode(
        `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
      ),
    );
  emit("token", { content: "first" });
  await until(() => session.messages.at(-1)?.content === "first");
  const user = session.messages[0];
  // The CJS fs object is also used by the CLI config store.
  const reads = mock.method(fs.default, "readFileSync");
  try {
    for (let i = 0; i < 4; i++) {
      emit("token", { content: "!" });
      await until(
        () => session.messages.at(-1)?.content === "first" + "!".repeat(i + 1),
      );
    }
    const configReads = reads.mock.calls.filter((call) =>
      String(call.arguments[0]).endsWith("config.json"),
    );
    assert.equal(
      configReads.length,
      0,
      "Token updates must not read credentials from disk",
    );
    assert.equal(
      session.messages[0],
      user,
      "Unchanged user nodes must retain identity",
    );
  } finally {
    emit("done", {});
    body.close();
    await sending;
  }
});

test("stream trace IDs reach both message nodes and persisted branches", async () => {
  const { useTerminalSession } = await import("./session");
  saveConfig({ ...loadConfig()!, thread_id: null });
  mock.method(globalThis, "fetch", async () =>
    response([
      ["trace", { thread_id: "assigned-thread" }],
      ["token", { content: "assigned" }],
      ["trace", { thread_id: "resolved-thread" }],
      ["token", { content: " answer" }],
      ["done", {}],
    ]),
  );
  let session!: ReturnType<typeof useTerminalSession>;
  function Probe() {
    session = useTerminalSession([]);
    return null;
  }
  render(<Probe />);
  await until(() => !!session);
  await session.onSend("first prompt");
  await until(() => session.messages.at(-1)?.content === "assigned answer");
  assert.deepEqual(
    session.messages.map((m) => m.threadId),
    ["resolved-thread", "resolved-thread"],
  );
  assert.equal(loadConfig()?.thread_id, "resolved-thread");
  assert.equal((await loadBranches("resolved-thread"))?.nodes.length, 2);
});

test("offline startup selects the configured thread in a shared saved graph", async () => {
  const { reconcileHistory, visibleMessages } = await import("./session");
  const saved: BranchHistory = {
    headId: "sibling",
    nodes: [
      { parentId: null, message: initial[0] },
      { parentId: "u1", message: initial[1] },
      {
        parentId: null,
        message: {
          ...initial[1],
          runtimeId: "sibling",
          threadId: "thread-2",
          content: "Other branch",
        },
      },
    ],
  };
  // Missing hydration is distinct from an authoritative empty server transcript.
  assert.deepEqual(
    visibleMessages(reconcileHistory(undefined, saved, "thread-1")),
    initial,
  );
  assert.deepEqual(
    visibleMessages(reconcileHistory([], saved, "thread-1")),
    [],
  );
  assert.deepEqual(
    visibleMessages(reconcileHistory(undefined, undefined, "thread-1")),
    [],
  );
});

test("thread discovery paginates workspaces directly and publishes each page", async () => {
  const { listThreads } = await import("./services");
  const calls: string[] = [];
  const pages: string[][] = [];
  mock.method(globalThis, "fetch", async (url: RequestInfo | URL) => {
    const path = new URL(String(url)).pathname + new URL(String(url)).search;
    calls.push(path);
    if (path.endsWith("/workspaces"))
      return Response.json([{ id: "first" }, { id: "second" }]);
    assert.match(
      path,
      /\/workspaces\/(first|second)\/threads\?limit=100&page=\d/,
    );
    const firstPage = path.includes("first") && path.endsWith("page=1");
    return Response.json({
      threads: [
        {
          id: firstPage ? "older" : path.includes("first") ? "newer" : "fork",
          updated_at: firstPage ? "2025" : "2026",
        },
      ],
      has_more: firstPage,
    });
  });
  const result = await listThreads(undefined, (entries) =>
    pages.push(entries.map((t) => t.id)),
  );
  assert.equal(calls.length, 4, "Listing must not walk every conversation");
  assert.deepEqual(pages, [
    ["older"],
    ["newer", "older"],
    ["newer", "fork", "older"],
  ]);
  assert.equal(result.length, 3);
});

test("thread loading preserves drafts and Escape restores sending", async () => {
  let release!: () => void;
  const sent: string[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/workspaces"))
        return Response.json([{ id: "workspace" }]);
      if (path.endsWith("page=1"))
        return Response.json({
          threads: [
            {
              id: "thread-2",
              title: "First page thread",
              status: "active",
              updated_at: "2026",
            },
          ],
          has_more: true,
        });
      if (path.endsWith("page=2")) {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return Response.json({
          threads: [
            {
              id: "late",
              title: "Late thread",
              status: "active",
              updated_at: "2026",
            },
          ],
          has_more: false,
        });
      }
      assert.ok(path.endsWith("/agent/stream"));
      sent.push(JSON.parse(String(init?.body)).messages.at(-1).content);
      return response([
        ["token", { content: "Received your draft" }],
        ["done", {}],
      ]);
    },
  );
  const ui = await mount();
  await key(ui, "/threads");
  await key(ui, "\r");
  await until(
    () => !!release && !!ui.lastFrame()?.includes("First page thread"),
  );
  assert.match(ui.lastFrame()!, /Loading threads… 1 loaded/);
  await key(ui, "/history 5");
  await key(ui, "\r");
  assert.match(ui.lastFrame()!, /Your draft is retained/);
  assert.match(
    ui.lastFrame()!,
    /\/history 5/,
    "A busy command must not erase the next slash draft",
  );
  assert.deepEqual(sent, []);
  await key(ui, "\x1b");
  release();
  await delay(100);
  assert.doesNotMatch(ui.lastFrame()!, /Late thread|Loading threads/);
  await key(ui, "\x15");
  await key(ui, "hi");
  await key(ui, "\r");
  await until(() => sent.length === 1);
  assert.deepEqual(sent, ["hi"]);
});

test("cancelled thread pages never publish late results", async () => {
  const { listThreads } = await import("./services");
  const controller = new AbortController();
  let release!: () => void;
  let pages = 0;
  mock.method(globalThis, "fetch", async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/workspaces"))
      return Response.json([{ id: "workspace" }]);
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    return Response.json({
      threads: [{ id: "late", updated_at: "2026" }],
      has_more: false,
    });
  });
  const result = listThreads(controller.signal, () => {
    pages++;
  }).catch((error) => error);
  await until(() => !!release);
  controller.abort();
  release();
  const error = await result;
  assert.equal(
    pages,
    0,
    "Cancelled thread pages must not publish into the next command",
  );
  assert.ok(error instanceof Error);
});
