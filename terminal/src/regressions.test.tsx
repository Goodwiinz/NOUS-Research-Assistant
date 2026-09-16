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
