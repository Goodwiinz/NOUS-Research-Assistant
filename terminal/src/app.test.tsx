import assert from "node:assert/strict";
import { afterEach, beforeEach, mock, test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { createRequire } from "node:module";
import { render, cleanup } from "ink-testing-library";
import type { RuntimeMessage } from "@nous/chat-runtime/types";
import { saveConfig } from "../../frontend/cli/auth/store";
import { streamReply, terminalText, type Approve } from "./adapter";
import { App } from "./app";
import { useTerminalSession } from "./session";

let configDir: string;
const originalConfigDir = process.env.NOUS_CONFIG_DIR;
const originalApi = process.env.NOUS_API_URL;
beforeEach(() => {
  configDir = mkdtempSync(join(tmpdir(), "nous-ink-test-"));
  process.env.NOUS_CONFIG_DIR = configDir;
  process.env.NOUS_API_URL = "https://nous.invalid/api/v1";
  saveConfig({
    token: "test-token",
    user_email: "test@example.com",
    organization_id: "org",
    expires_at: "2099-01-01",
    thread_id: "thread-1",
    project_id: "project-1",
    project_name: "Research",
  });
});
afterEach(() => {
  cleanup();
  mock.restoreAll();
  if (originalConfigDir === undefined) delete process.env.NOUS_CONFIG_DIR;
  else process.env.NOUS_CONFIG_DIR = originalConfigDir;
  if (originalApi === undefined) delete process.env.NOUS_API_URL;
  else process.env.NOUS_API_URL = originalApi;
  rmSync(configDir, { recursive: true, force: true });
});

function response(events: [string, unknown][]) {
  return new Response(
    events
      .map(
        ([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
      )
      .join(""),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

function run(approve: Approve, signal = new AbortController().signal) {
  return streamReply("Find papers", "reply-1", signal, approve);
}

async function collect(stream: AsyncGenerator<RuntimeMessage>) {
  const updates: RuntimeMessage[] = [];
  for await (const update of stream) updates.push(update);
  return updates;
}

async function until(predicate: () => boolean, description: string) {
  const deadline = Date.now() + 5000;
  while (!predicate() && Date.now() < deadline) await delay(10);
  assert.ok(predicate(), description);
}

test("React resolves independently for the web and terminal", () => {
  const web = createRequire(
    new URL("../../frontend/package.json", import.meta.url),
  );
  const terminal = createRequire(import.meta.url);
  assert.equal(web("react/package.json").version, "18.3.1");
  assert.equal(web("react-dom/package.json").version, "18.3.1");
  assert.equal(terminal("react").version, "19.2.8");
  for (const name of ["ink", "@assistant-ui/react-ink"]) {
    assert.equal(
      createRequire(terminal.resolve(name))("react"),
      terminal("react"),
    );
  }
});

test("real CLI transport preserves auth/context and renders revised text and tool results", async () => {
  const fetch = mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      assert.equal(
        new Headers(init?.headers).get("Authorization"),
        "Bearer test-token",
      );
      const body = JSON.parse(String(init?.body));
      assert.equal(body.thread_id, "thread-1");
      assert.equal(body.page_context.project_id, "project-1");
      return response([
        ["tool_start", { tool: "search", args: '{"query":"papers"}' }],
        ["token", { content: "Bad draft" }],
        ["reflection", { revising: true, passed: false }],
        ["tool_end", { tool: "search", result: "Found two papers" }],
        ["token", { content: "Revised answer" }],
        ["done", {}],
      ]);
    },
  );
  const updates = await collect(
    run(async () => assert.fail("Unexpected approval")),
  );
  const final = updates.at(-1)!;
  assert.equal(final.isStreaming, false);
  assert.equal(final.content, "Revised answer");
  const tool = final.toolExecutions?.[0];
  assert.equal(tool?.tool, "search");
  assert.equal(tool?.result, "Found two papers");
  assert.equal(fetch.mock.callCount(), 1);
});

for (const approved of [true, false]) {
  test(`Ink requires explicit ${approved ? "approval" : "denial"} and posts it once`, async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    mock.method(
      globalThis,
      "fetch",
      async (url: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(url), body: JSON.parse(String(init?.body)) });
        return calls.length === 1
          ? response([
              [
                "confirmation",
                {
                  thread_id: "thread-1",
                  confirmation: {
                    tool_name: "create_note",
                    tool_args: { title: "Review me" },
                  },
                },
              ],
            ])
          : response([
              [
                "token",
                { content: approved ? "Note created" : "Request denied" },
              ],
              ["done", {}],
            ]);
      },
    );
    const ui = render(<App />);
    await until(
      () =>
        ui.frames.length > 1 && (ui.lastFrame()?.includes("Ask NOUS") ?? false),
      "Composer mounts",
    );
    ui.stdin.write("Find papers");
    await until(
      () => ui.lastFrame()?.includes("Find papers") ?? false,
      "Input is visible",
    );
    ui.stdin.write("\r");
    await until(
      () => ui.lastFrame()?.includes("Approval required") ?? false,
      "Approval is visible",
    );
    assert.match(ui.lastFrame()!, /Review me/);
    ui.stdin.write("\x1b");
    await delay(60);
    ui.stdin.write("\r");
    await delay(30);
    assert.equal(calls.length, 1, "Enter alone does not approve");
    const framesBeforeDecision = ui.frames.length;
    ui.stdin.write(approved ? "yes" : "no");
    await until(
      () => ui.frames.length > framesBeforeDecision,
      "Decision is typed",
    );
    ui.stdin.write("\r");
    await until(
      () =>
        ui
          .lastFrame()
          ?.includes(approved ? "Note created" : "Request denied") ?? false,
      "Resumed response renders",
    );
    assert.equal(calls.length, 2);
    assert.match(calls[1].url, /\/agent\/stream\/confirm$/);
    assert.deepEqual(calls[1].body, {
      thread_id: "thread-1",
      confirmed: approved,
    });
    assert.doesNotMatch(ui.lastFrame()!, /Approval required/);
    await delay(60);
    ui.stdin.write("/help");
    await delay(40);
    ui.stdin.write("\r");
    await until(
      () => !!ui.lastFrame()?.includes("Edits and retries create"),
      "Composer usable after approval settles",
    );
    ui.unmount();
  });
}

test("Ctrl+C during approval cancels without posting a decision", async () => {
  const fetch = mock.method(globalThis, "fetch", async () =>
    response([
      [
        "confirmation",
        { thread_id: "thread-1", confirmation: { tool_name: "create_note" } },
      ],
    ]),
  );
  const ui = render(<App />);
  await until(
    () =>
      ui.frames.length > 1 && (ui.lastFrame()?.includes("Ask NOUS") ?? false),
    "Composer mounts",
  );
  ui.stdin.write("Find papers");
  await until(
    () => ui.lastFrame()?.includes("Find papers") ?? false,
    "Input is visible",
  );
  ui.stdin.write("\r");
  await until(
    () => ui.lastFrame()?.includes("Approval required") ?? false,
    "Approval is visible",
  );
  ui.stdin.write("\x03");
  await until(
    () => ui.lastFrame()?.includes("cancelled") ?? false,
    "Cancellation settles",
  );
  assert.equal(fetch.mock.callCount(), 1);
  assert.doesNotMatch(ui.lastFrame()!, /Approval required/);
  ui.unmount();
});

test("incomplete and failed streams fail without retrying", async () => {
  for (const events of [
    [["token", { content: "Partial answer" }]],
    [["error", { error: "Backend unavailable" }]],
  ] as [string, unknown][][]) {
    const fetch = mock.method(globalThis, "fetch", async () =>
      response(events),
    );
    await assert.rejects(
      collect(run(async () => false)),
      /before completion|Backend unavailable/,
    );
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.restore();
  }
  assert.equal(terminalText("\x1b[2Jhello\x1b]52;c;secret\x07\r\x00"), "hello");
});

test("Ctrl+C aborts an active HTTP stream and leaves the composer usable", async () => {
  let signal: AbortSignal | undefined;
  mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      signal = init?.signal ?? undefined;
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: token\ndata: {"content":"Partial answer"}\n\n',
              ),
            );
            signal?.addEventListener(
              "abort",
              () => controller.error(new DOMException("Stopped", "AbortError")),
              { once: true },
            );
          },
        }),
      );
    },
  );
  const ui = render(<App />);
  await until(
    () =>
      ui.frames.length > 1 && (ui.lastFrame()?.includes("Ask NOUS") ?? false),
    "Composer mounts",
  );
  ui.stdin.write("Find papers");
  await until(
    () => ui.lastFrame()?.includes("Find papers") ?? false,
    "Input is visible",
  );
  ui.stdin.write("\r");
  await until(
    () => ui.lastFrame()?.includes("Partial answer") ?? false,
    "Stream is active",
  );
  ui.stdin.write("\x03");
  await until(
    () => ui.lastFrame()?.includes("cancelled") ?? false,
    "Stream settles after cancellation",
  );
  assert.ok(signal?.aborted);
  assert.match(ui.lastFrame()!, /Ask NOUS/);
  ui.unmount();
});

test("an uncertain approval response is never posted again", async () => {
  const fetch = mock.method(globalThis, "fetch", async () => {
    if (fetch.mock.callCount() === 0)
      return response([
        [
          "confirmation",
          { thread_id: "thread-1", confirmation: { tool_name: "create_note" } },
        ],
      ]);
    throw new Error("Connection lost after posting");
  });
  await assert.rejects(collect(run(async () => true)), /Connection lost/);
  assert.equal(fetch.mock.callCount(), 2);
});

test("a queued turn waits for approval and the preceding response to finish", async () => {
  let first!: ReadableStreamDefaultController<Uint8Array>;
  const paths: string[] = [];
  mock.method(globalThis, "fetch", async (url: RequestInfo | URL) => {
    paths.push(String(url));
    if (paths.length === 1)
      return new Response(
        new ReadableStream({
          start(controller) {
            first = controller;
          },
        }),
      );
    return response([
      [
        "token",
        { content: paths.length === 2 ? "Denied first" : "Second answer" },
      ],
      ["done", {}],
    ]);
  });
  const ui = render(<App />);
  await until(() => ui.frames.length > 1, "Composer is focused");
  ui.stdin.write("first");
  await until(
    () => ui.lastFrame()?.includes("first") ?? false,
    "First prompt is typed",
  );
  ui.stdin.write("\r");
  await until(
    () =>
      paths.length === 1 &&
      !!ui.lastFrame()?.includes("Working…") &&
      !!ui.lastFrame()?.includes("Ask NOUS"),
    "First request starts",
  );
  ui.stdin.write("second");
  await until(
    () => ui.lastFrame()?.includes("second") ?? false,
    "Second prompt is typed",
  );
  ui.stdin.write("\r");
  await until(
    () => ui.lastFrame()?.includes("Queued: second") ?? false,
    "Follow-up is queued",
  );
  assert.equal(paths.length, 1);
  first.enqueue(
    new TextEncoder().encode(
      'event: confirmation\ndata: {"thread_id":"thread-1","confirmation":{"tool_name":"create_note"}}\n\n',
    ),
  );
  first.close();
  await until(
    () => ui.lastFrame()?.includes("Approval required") ?? false,
    "Approval blocks the queue",
  );
  assert.equal(paths.length, 1);
  // Approval mounts and claims focus across multiple React commits.
  await delay(150);
  const frames = ui.frames.length;
  ui.stdin.write("no");
  await until(() => ui.frames.length > frames, "Decision is typed");
  ui.stdin.write("\r");
  await until(
    () => ui.lastFrame()?.includes("Second answer") ?? false,
    "Queue resumes after the first turn finishes",
  );
  assert.equal(paths.length, 3);
  assert.match(paths[1], /stream\/confirm$/);
  assert.match(paths[2], /agent\/stream$/);
  ui.unmount();
});

test("an earlier approval cannot authorize a later gate", async () => {
  let calls = 0;
  mock.method(globalThis, "fetch", async () => {
    calls++;
    return calls < 3
      ? response([
          [
            "confirmation",
            {
              thread_id: "thread-1",
              confirmation: { tool_name: "create_note" },
            },
          ],
        ])
      : response([["done", {}]]);
  });
  let session!: ReturnType<typeof useTerminalSession>;
  function Probe() {
    session = useTerminalSession([]);
    return null;
  }
  const ui = render(<Probe />);
  await until(() => !!session, "Session mounts");
  const turn = session.onSend("first");
  const gate = () =>
    session.messages.find((message) => message.pendingApproval)?.pendingApproval
      ?.id;
  await until(() => !!gate(), "First gate opens");
  const oldId = gate()!;
  session.onApproval(true, oldId);
  await until(() => !!gate() && gate() !== oldId, "Second gate opens");
  // Mutation check: remove the approvalId guard in terminal/src/session.ts;
  // this test must send a third request before a fresh decision and fail.
  session.onApproval(true, oldId);
  await delay(20);
  assert.equal(calls, 2);
  session.onApproval(false, gate());
  await turn;
  assert.equal(calls, 3);
  ui.unmount();
});

test("edit branches preserve the original server thread and route later sends to the selected branch", async () => {
  const { loadConfig } = await import("../../frontend/cli/auth/store");
  const { loadBranches } = await import("./services");
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      const target = String(url);
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      calls.push({ url: target, body });
      if (target.includes("/api/v2/threads/thread-1?"))
        return Response.json({
          id: "thread-1",
          conversation_id: "conversation-1",
          title: "Original",
        });
      if (target.endsWith("/api/v2/threads"))
        return Response.json({ id: "branch-2" });
      return response([
        ["token", { content: "Branch answer" }],
        ["done", {}],
      ]);
    },
  );
  let session!: ReturnType<typeof useTerminalSession>;
  const original = [
    {
      runtimeId: "u1",
      threadId: "thread-1",
      role: "user" as const,
      content: "Original question",
      timestamp: 1,
    },
    {
      runtimeId: "a1",
      threadId: "thread-1",
      role: "assistant" as const,
      content: "Original answer",
      timestamp: 2,
    },
  ];
  function Probe() {
    session = useTerminalSession(original);
    return null;
  }
  const ui = render(<Probe />);
  await until(() => !!session, "Session mounted");
  await session.branch("u1", "Edited question");
  await until(
    () => session.messages[0]?.content === "Edited question",
    "New branch displayed",
  );
  assert.equal(loadConfig()?.thread_id, "branch-2");
  assert.ok(
    session.history.nodes.some((n) => n.message.content === "Original answer"),
  );
  const sent = calls.find((c) => c.url.endsWith("/agent/stream"))!;
  assert.equal(sent.body.thread_id, "branch-2");
  assert.equal(
    (sent.body.messages as Array<{ content: string }>)[0].content,
    "Edited question",
  );
  assert.ok(await loadBranches("thread-1"));
  session.switchBranch(["u1", "a1"]);
  await until(
    () => session.messages[0]?.content === "Original question",
    "Original selected",
  );
  await session.onSend("Continue original");
  assert.equal(calls.at(-1)?.body.thread_id, "thread-1");
  assert.equal(
    (calls.at(-1)?.body.messages as Array<{ content: string }>)[0].content,
    "Original question",
  );
  ui.unmount();
});

test("stale thread loads cannot overwrite the latest selection", async () => {
  let finish!: (response: Response) => void;
  mock.method(globalThis, "fetch", async (url: RequestInfo | URL) => {
    if (String(url).includes("/slow/"))
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return Response.json({
      messages: [
        {
          id: "fast-message",
          thread_id: "fast",
          role: "assistant",
          content: "Latest thread",
          created_at: "",
          attachments: [],
          citations: [],
        },
      ],
      has_more: false,
    });
  });
  let session!: ReturnType<typeof useTerminalSession>;
  function Probe() {
    session = useTerminalSession([]);
    return null;
  }
  const ui = render(<Probe />);
  await until(() => !!session, "Session mounted");
  const slow = session.switchThread("slow");
  await until(() => !!finish, "Slow load pending");
  await session.switchThread("fast");
  finish(Response.json({ messages: [], has_more: false }));
  await slow;
  // Let the stale completion commit; checking the already-rendered fast result would be a false positive.
  await delay(30);
  await until(
    () => session.messages[0]?.content === "Latest thread",
    "Latest selection retained",
  );
  ui.unmount();
});

test("uploads use authenticated multipart data, preserve document IDs, and detach without deleting documents", async () => {
  const { attachmentAdapter } = await import("./services");
  const calls: RequestInit[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init!);
      assert.equal(
        new Headers(init?.headers).get("Authorization"),
        "Bearer test-token",
      );
      assert.equal(new Headers(init?.headers).has("Content-Type"), false);
      assert.ok(init?.body instanceof FormData);
      assert.equal(init.body.get("title"), "notes.txt");
      return Response.json({ document_id: "document-1" });
    },
  );
  const pending = await attachmentAdapter.add({
    file: new File(["Research notes"], "notes.txt"),
  });
  assert.ok("id" in pending);
  const complete = await attachmentAdapter.send(
    pending as import("@assistant-ui/core").PendingAttachment,
  );
  assert.equal(complete.id, "document-1");
  assert.equal(complete.status.type, "complete");
  await attachmentAdapter.remove(complete);
  assert.equal(calls.length, 1);
});

test("multiline composer sends quotes and attachments through the actual runtime queue", async () => {
  const bodies: Array<Record<string, unknown>> = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/documents/"))
        return Response.json({ id: "doc-7", title: "Notes" });
      bodies.push(JSON.parse(String(init?.body)));
      return response([
        ["token", { content: "Received" }],
        ["done", {}],
      ]);
    },
  );
  const ui = render(
    <App
      initialMessages={[
        {
          runtimeId: "prior",
          role: "assistant",
          content: "Quoted evidence",
          timestamp: 1,
        },
      ]}
    />,
  );
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  const type = async (text: string) => {
    ui.stdin.write(text);
    await delay(40);
    ui.stdin.write("\r");
    await delay(40);
  };
  await type("/document doc-7");
  await until(() => !!ui.lastFrame()?.includes("Notes"), "Attachment visible");
  await type("/quote 1");
  await until(() => !!ui.lastFrame()?.includes("Quoting:"), "Quote visible");
  ui.stdin.write("First line");
  await delay(30);
  ui.stdin.write("\n");
  ui.stdin.write("Second line");
  await delay(30);
  ui.stdin.write("\r");
  await until(() => bodies.length === 1, "Request dispatched");
  assert.deepEqual(bodies[0].attachment_ids, ["doc-7"]);
  const messages = bodies[0].messages as Array<{ content: string }>;
  assert.match(messages.at(-1)!.content, /Quoted evidence/);
  assert.match(messages.at(-1)!.content, /First line\nSecond line/);
  ui.unmount();
});

test("edit composer, branch picker, and thread list controls operate through the Ink runtime", async () => {
  const { loadConfig } = await import("../../frontend/cli/auth/store");
  const initial = [
    {
      runtimeId: "u1",
      threadId: "thread-1",
      role: "user" as const,
      content: "Original question",
      timestamp: 1,
    },
    {
      runtimeId: "a1",
      threadId: "thread-1",
      role: "assistant" as const,
      content: "Original answer",
      timestamp: 2,
    },
  ];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/workspaces"))
        return Response.json([{ id: "workspace-1" }]);
      if (path.includes("/conversations?"))
        return Response.json({
          conversations: [{ id: "conversation-1" }],
          has_more: false,
        });
      if (path.includes("/threads?limit="))
        return Response.json({
          threads: [
            {
              id: "thread-1",
              title: "Original thread",
              status: "active",
              updated_at: "",
            },
            {
              id: "archived-1",
              title: "Archived thread",
              status: "archived",
              updated_at: "",
            },
          ],
        });
      if (path.includes("/api/v2/threads/thread-1?"))
        return Response.json({
          conversation_id: "conversation-1",
          title: "Original",
        });
      if (path.endsWith("/api/v2/threads"))
        return Response.json({ id: "branch-2" });
      if (path.endsWith("/agent/stream")) {
        const body = JSON.parse(String(init?.body));
        assert.equal(body.messages.at(-1).content, "Edited question");
        assert.equal(
          body.thread_id,
          "branch-2",
          "Edit must send to the new branch",
        );
        assert.equal(
          body.messages.length,
          1,
          "Original branch history is untouched",
        );
        return response([
          ["token", { content: "Alternate answer" }],
          ["done", {}],
        ]);
      }
      throw new Error(`Unexpected URL ${path}`);
    },
  );
  const ui = render(<App initialMessages={initial} />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/edit 1");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Edit message"),
    "Edit composer opened",
  );
  await delay(40);
  ui.stdin.write("\x01");
  ui.stdin.write("\x0b");
  ui.stdin.write("Edited question");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Alternate answer"),
    "Edit produced new branch",
  );
  assert.equal(loadConfig()?.thread_id, "branch-2");
  await delay(40);
  ui.stdin.write("/branch 1 previous");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Original answer"),
    "Branch picker restored original",
  );
  assert.equal(loadConfig()?.thread_id, "thread-1");
  await delay(40);
  ui.stdin.write("/threads");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Archived thread"),
    "Thread panel includes archive actions",
  );
  assert.match(ui.lastFrame()!, /Restore/);
  ui.unmount();
});

test("thread deletion requires an explicit typed confirmation and feedback writes the canonical message ID", async () => {
  const requests: Array<{ method: string; url: string; body: unknown }> = [];
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      requests.push({
        method: init?.method ?? "GET",
        url: String(url),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      if (init?.method === "PATCH") return Response.json({});
      return Response.json(
        String(url).endsWith("/workspaces") ? [] : { threads: [] },
      );
    },
  );
  const ui = render(
    <App
      initialMessages={[
        {
          runtimeId: "reply",
          serverId: "db-reply",
          threadId: "thread-1",
          role: "assistant",
          content: "Saved answer",
          timestamp: 1,
        },
      ]}
    />,
  );
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/like 1");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => requests.some((r) => r.method === "PATCH"),
    "Feedback sent",
  );
  assert.match(requests[0].url, /\/messages\/db-reply$/);
  assert.deepEqual(requests[0].body, { feedback_rating: 5 });
  await delay(40);
  ui.stdin.write("/delete");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Type delete to confirm"),
    "Delete confirmation visible",
  );
  await delay(40);
  ui.stdin.write("\r");
  await delay(40);
  assert.ok(!requests.some((r) => r.method === "DELETE"));
  await delay(40);
  ui.stdin.write("delete");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => requests.some((r) => r.method === "DELETE"),
    "Explicit confirmation deletes",
  );
  assert.equal(requests.filter((r) => r.method === "DELETE").length, 1);
  await until(
    () =>
      !ui.lastFrame()?.includes("Type delete to confirm") &&
      !!ui.lastFrame()?.includes("Ask NOUS"),
    "Deletion and thread refresh settle",
  );
  ui.unmount();
});

test("a fast completed response does not leave the queue busy for the next send", async () => {
  let calls = 0;
  mock.method(globalThis, "fetch", async () => {
    calls++;
    return response([
      ["token", { content: `Answer ${calls}` }],
      ["done", {}],
    ]);
  });
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  for (const prompt of ["one", "two"]) {
    ui.stdin.write(prompt);
    await delay(40);
    ui.stdin.write("\r");
    await until(
      () =>
        !!ui.lastFrame()?.includes(prompt === "one" ? "Answer 1" : "Answer 2"),
      "Fast answer rendered",
    );
    await delay(40);
  }
  assert.equal(calls, 2);
  ui.unmount();
});

test("invalid sends recover and invalid edits never create a server branch", async () => {
  const fetch = mock.method(globalThis, "fetch", async () =>
    response([
      ["token", { content: "Recovered" }],
      ["done", {}],
    ]),
  );
  let session!: ReturnType<typeof useTerminalSession>;
  function Harness() {
    session = useTerminalSession([]);
    return null;
  }
  const ui = render(<Harness />);
  await until(() => !!session, "Session mounts");
  await session.onSend("x".repeat(32001));
  await until(
    () => !session.isRunning && !!session.messages.at(-1)?.error,
    "Validation failure settles",
  );
  assert.equal(fetch.mock.callCount(), 0);
  const userId = session.messages[0].runtimeId;
  await assert.rejects(session.branch(userId, "x".repeat(32001)), /32,000/);
  assert.equal(fetch.mock.callCount(), 0);
  await session.onSend("valid");
  await until(
    () =>
      !session.isRunning && session.messages.at(-1)?.content === "Recovered",
    "Next send succeeds",
  );
  assert.equal(fetch.mock.callCount(), 1);
  ui.unmount();
});

test("Ink restores and persists the original CLI draft and clears it after sending", async () => {
  const { readDraft, writeDraft } =
    await import("../../frontend/cli/services/draft");
  writeDraft("Recovered draft");
  mock.method(globalThis, "fetch", async () =>
    response([
      ["token", { content: "Done" }],
      ["done", {}],
    ]),
  );
  const ui = render(<App />);
  await until(
    () => !!ui.lastFrame()?.includes("Recovered draft"),
    "Original draft restored",
  );
  ui.stdin.write(" extended");
  await until(
    () => readDraft() === "Recovered draft extended",
    "Draft persisted",
  );
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Done") && readDraft() === "",
    "Submitted draft cleared",
  );
  ui.unmount();
});

test("changing the backend clears the previous token before exiting for login", async () => {
  const { loadConfig } = await import("../../frontend/cli/auth/store");
  delete process.env.NOUS_API_URL;
  const fetch = mock.method(globalThis, "fetch", async () => {
    throw new Error("Unexpected request");
  });
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/settings set api_url https://new.invalid/api/v1");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => loadConfig()?.api_url === "https://new.invalid/api/v1",
    "Backend saved",
  );
  assert.equal(loadConfig()?.token, "");
  assert.equal(loadConfig()?.thread_id, null);
  assert.equal(loadConfig()?.project_id, null);
  assert.equal(fetch.mock.callCount(), 0);
  ui.unmount();
});

test("typing slash opens and filters commands; arrows and Enter complete without sending", async () => {
  const fetch = mock.method(globalThis, "fetch", async () => {
    throw new Error("Unexpected request");
  });
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/");
  await until(
    () => !!ui.lastFrame()?.includes("Commands ("),
    "Slash opens command list immediately",
  );
  assert.match(ui.lastFrame()!, /Show commands and keyboard controls/);
  ui.stdin.write("pro");
  await until(
    () => !!ui.lastFrame()?.includes("Commands (2)"),
    "Prefix filters commands",
  );
  assert.match(ui.lastFrame()!, /Choose a project/);
  assert.doesNotMatch(ui.lastFrame()!, /Show commands and keyboard controls/);
  ui.stdin.write("\x1b[B");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () =>
      !!ui.lastFrame()?.includes("/project ") &&
      !ui.lastFrame()?.includes("Commands ("),
    "Selected command completes for arguments",
  );
  assert.equal(fetch.mock.callCount(), 0);
  ui.unmount();
});

test("slash completion dismisses on Escape and leaves ordinary text and exact commands usable", async () => {
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/se");
  await until(
    () => !!ui.lastFrame()?.includes("Commands ("),
    "Suggestions visible",
  );
  ui.stdin.write("\x1b");
  await until(
    () => !ui.lastFrame()?.includes("Commands ("),
    "Escape hides list",
  );
  assert.match(ui.lastFrame()!, /\/se/);
  await delay(40);
  ui.stdin.write("t");
  await until(
    () => !!ui.lastFrame()?.includes("Commands (1)"),
    "Editing reopens filtered list",
  );
  ui.stdin.write("\x15");
  await delay(40);
  ui.stdin.write("ordinary / text");
  await delay(40);
  assert.doesNotMatch(ui.lastFrame()!, /Commands \(/);
  ui.stdin.write("\x15");
  await delay(40);
  ui.stdin.write("/settings");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Settings ·"),
    "Exact command still runs on Enter",
  );
  ui.unmount();
});

test("sending remains usable after slash help and menu cancellation", async () => {
  const sent: string[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      sent.push(body.messages.at(-1).content);
      return response([
        ["token", { content: `Answer ${sent.length}` }],
        ["done", {}],
      ]);
    },
  );
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("\x1b");
  // Let Escape parsing and the input remount finish before typing.
  await delay(150);
  for (const command of ["/help", "/settings"]) {
    ui.stdin.write(command);
    await delay(40);
    ui.stdin.write("\r");
    await until(
      () =>
        !!ui
          .lastFrame()
          ?.includes(
            command === "/help" ? "Edits and retries create" : "Settings ·",
          ),
      "Slash action opens",
    );
    if (command === "/help") {
      ui.stdin.write("hi");
      await delay(40);
      ui.stdin.write("\t"); // Escape must return from a control and keep the draft.
      await delay(40);
    }
    ui.stdin.write("\x1b");
    await delay(150);
    if (command !== "/help") ui.stdin.write("hi");
    await delay(40);
    ui.stdin.write("\r");
    await until(
      () => sent.length === (command === "/help" ? 1 : 2),
      `Can send after ${command}`,
    );
    await until(
      () => !!ui.lastFrame()?.includes(`Answer ${sent.length}`),
      "Answer visible",
    );
    await delay(150);
  }
  assert.deepEqual(sent, ["hi", "hi"]);
  ui.unmount();
});

test("dismissed slash reads cannot reopen menus or lock subsequent commands", async () => {
  let release!: () => void;
  let releaseNext!: () => void;
  let signal: AbortSignal | undefined;
  mock.method(
    globalThis,
    "fetch",
    async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/projects")) {
        signal = init?.signal as AbortSignal;
        // Ignore abort deliberately: verify the stale-result guard as well.
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return Response.json({
          projects: [{ id: "late", name: "Late project" }],
        });
      }
      await new Promise<void>((resolve) => {
        releaseNext = resolve;
      });
      return Response.json({
        documents: [{ id: "doc", title: "Selected result" }],
      });
    },
  );
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/projects");
  await delay(40);
  ui.stdin.write("\r");
  await until(() => !!release, "Read started");
  ui.stdin.write("\x1b");
  // Let Escape parsing and the input remount finish before typing.
  await delay(150);
  ui.stdin.write("/documents");
  await delay(40);
  ui.stdin.write("\r");
  await until(() => !!releaseNext, "Next command usable after cancellation");
  release();
  await delay(80);
  assert.ok(signal?.aborted, "Cancelled read is aborted");
  assert.doesNotMatch(
    ui.lastFrame()!,
    /Pick a project/,
    "Late result must not reopen cancelled menu",
  );
  assert.match(
    ui.lastFrame()!,
    /Working on command/,
    "Old command must not unlock the newer operation",
  );
  releaseNext();
  await until(
    () => !!ui.lastFrame()?.includes("Selected result"),
    "Newer command completes",
  );
  await delay(40);
  ui.stdin.write("/help");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Edits and retries create"),
    "Subsequent command usable",
  );
  ui.unmount();
});

test("multiline history recalls whole prompts", async () => {
  const { appendHistory } =
    await import("../../frontend/cli/services/promptHistory");
  appendHistory("First line\nSecond line");
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("\x10");
  await until(
    () => !!ui.lastFrame()?.includes("Second line"),
    "History recalled",
  );
  assert.match(ui.lastFrame()!, /First line/);
  ui.unmount();
});

test("Escape cancels editing and returns keyboard input to chat", async () => {
  const calls: unknown[] = [];
  mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(JSON.parse(String(init?.body)));
      return response([
        ["token", { content: "After cancelling edit" }],
        ["done", {}],
      ]);
    },
  );
  const ui = render(
    <App
      initialMessages={[
        {
          runtimeId: "u1",
          threadId: "thread-1",
          role: "user",
          content: "Original",
          timestamp: 1,
        },
        {
          runtimeId: "a1",
          threadId: "thread-1",
          role: "assistant",
          content: "Original answer",
          timestamp: 2,
        },
      ]}
    />,
  );
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/edit 1");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("Edit message"),
    "Editor opened",
  );
  await delay(40);
  ui.stdin.write(" changed");
  await delay(40);
  ui.stdin.write("\x1b");
  await until(
    () => !ui.lastFrame()?.includes("Edit message"),
    "Editor cancelled",
  );
  await delay(60);
  ui.stdin.write("hi");
  await delay(40);
  ui.stdin.write("\r");
  await until(
    () => !!ui.lastFrame()?.includes("After cancelling edit"),
    "Chat receives input after cancellation",
  );
  assert.equal(calls.length, 1);
  assert.equal((calls[0] as { thread_id: string }).thread_id, "thread-1");
  assert.match(ui.lastFrame()!, /Original answer/);
  ui.unmount();
});

test("Escape during a submitted write waits for its result without retrying", async () => {
  let finish!: () => void;
  let writes = 0;
  mock.method(
    globalThis,
    "fetch",
    async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        writes++;
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
        return Response.json({ id: "thread-1" });
      }
      return Response.json([]);
    },
  );
  const ui = render(<App />);
  await until(
    () => ui.frames.length > 1 && !!ui.lastFrame()?.includes("Ask NOUS"),
    "Composer ready",
  );
  ui.stdin.write("/rename Renamed");
  await delay(40);
  ui.stdin.write("\r");
  await until(() => !!finish, "Write submitted");
  ui.stdin.write("\x1b");
  await delay(60);
  assert.match(ui.lastFrame()!, /Action already submitted/);
  assert.match(ui.lastFrame()!, /Working on command/);
  assert.equal(writes, 1);
  finish();
  await until(
    () => !ui.lastFrame()?.includes("Working on command"),
    "Submitted write settles",
  );
  assert.equal(writes, 1);
  assert.doesNotMatch(ui.lastFrame()!, /waiting for its result/);
  ui.unmount();
});
