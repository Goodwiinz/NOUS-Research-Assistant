import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { Journal } from "../src/journal.ts";
import { NativeExitUnconfirmed } from "../src/rpc.ts";
import type { BridgeCommand } from "../src/connection.ts";
import type { HarnessAdapter, SessionOptions } from "../src/contracts.ts";
const options: SessionOptions = {
  cwd: "/tmp",
  workspaceId: "local",
  policy: {
    sandbox: "workspace-write",
    approvalPolicy: "on-request",
    reviewer: "user",
    networkAccess: false,
    writableRoots: ["/tmp"],
  },
};
function command(): BridgeCommand {
  return {
    deviceId: randomUUID(),
    runId: randomUUID(),
    commandId: randomUUID(),
    workspaceId: randomUUID(),
    generation: 1,
    expiresAt: new Date(Date.now() + 60000).toISOString(),
    body: { kind: "start", input: "hello" },
  };
}
function fixture() {
  const dir = mkdtempSync(join(tmpdir(), "nous-journal-"));
  const path = join(dir, "journal.sqlite");
  return { path, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}
function adapter() {
  const value = {
    startCalls: 0,
    interrupts: [] as string[],
    async startSession() {
      return { id: "session" };
    },
    async startTurn() {
      this.startCalls++;
      throw new Error("response lost after acceptance");
    },
    async interruptTurn(s: string, t: string) {
      this.interrupts.push(`${s}/${t}`);
    },
  };
  return value as unknown as HarnessAdapter & {
    startCalls: number;
    interrupts: string[];
  };
}
test("ambiguous start is not replayed, including process restart", async () => {
  const f = fixture();
  const c = command();
  const a = adapter();
  let j = new Journal(f.path, () => options);
  try {
    await j.execute(c, a);
    await j.execute(c, a);
    assert.equal(a.startCalls, 1);
    assert.equal(j.state(c.commandId), "recovering");
    assert.equal(j.workspaceLocked(c.workspaceId), true);
    j.close();
    j = new Journal(f.path, () => options);
    await j.execute(c, a);
    assert.equal(a.startCalls, 1);
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    j.close();
    f.cleanup();
  }
});
test("expired offline lease interrupts exactly once and retains quarantine", async () => {
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const a = adapter();
  const c = command();
  try {
    await j.execute(c, a);
    await j.watchLease(new Date(0).toISOString(), a, "session", "turn");
    await j.watchLease(new Date(0).toISOString(), a, "session", "turn");
    assert.deepEqual(a.interrupts, ["session/turn"]);
    assert.equal(j.workspaceLocked(c.workspaceId), true);
    await assert.rejects(
      j.execute({ ...command(), workspaceId: c.workspaceId }, a),
    );
  } finally {
    j.close();
    f.cleanup();
  }
});

test("command mutation, generation mismatch, expiry, and wrong cancellation target fail closed", async () => {
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  try {
    await j.execute(c, a);
    await assert.rejects(
      j.execute({ ...c, body: { kind: "start", input: "mutated" } }, a),
      /conflict/,
    );
    await assert.rejects(
      j.execute(
        {
          ...c,
          commandId: randomUUID(),
          generation: 2,
          body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
        },
        a,
      ),
      /generation/,
    );
    await assert.rejects(
      j.execute(
        {
          ...c,
          commandId: randomUUID(),
          body: { kind: "interrupt", sessionId: "other", turnId: "turn" },
        },
        a,
      ),
      /target/,
    );
    await assert.rejects(
      j.execute({ ...command(), expiresAt: new Date(0).toISOString() }, a),
      /expired/,
    );
    assert.equal(a.startCalls, 1);
  } finally {
    j.close();
    f.cleanup();
  }
});
test("journal source events survive restart and only exact ordered committed acknowledgements unlock", async () => {
  const f = fixture();
  let j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  try {
    await j.execute(c, a);
    j.recordNative(c.commandId, {
      kind: "delta",
      sessionId: "session",
      turnId: "turn",
      text: "answer",
    });
    j.recordNative(c.commandId, {
      kind: "terminal",
      sessionId: "session",
      turnId: "turn",
      status: "completed",
    });
    const before = j.pending();
    assert.equal(j.workspaceLocked(c.workspaceId), true);
    j.close();
    j = new Journal(f.path, () => options);
    assert.deepEqual(j.pending(), before);
    const ack = (i: number) => ({
      runId: c.runId,
      sourceId: c.commandId,
      generation: c.generation,
      sourceSeq: before[i].sourceSeq,
      canonicalSeq: i + 1,
    });
    assert.throws(() => j.acknowledge(ack(1)), /gap/);
    assert.throws(
      () => j.acknowledge({ ...ack(0), generation: 2 }),
      /identity/,
    );
    for (let i = 0; i < before.length; i++) j.acknowledge(ack(i));
    assert.deepEqual(j.pending(), []);
    assert.equal(j.workspaceLocked(c.workspaceId), false);
    assert.equal(j.state(c.commandId), "completed");
  } finally {
    j.close();
    f.cleanup();
  }
});
test("matched native history fills missing output without replaying a start", async () => {
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  a.inspectTurn = async (s, id) => {
    assert.equal(id, c.commandId);
    return {
      state: "completed",
      sessionId: s,
      turnId: "turn",
      assistantText: "hello world",
    };
  };
  try {
    await j.execute(c, a);
    j.recordNative(c.commandId, {
      kind: "delta",
      sessionId: "session",
      turnId: "turn",
      text: "hello ",
    });
    await j.reconcile(c.commandId, a);
    const text = j
      .pending()
      .filter(
        (e) =>
          e.body.kind === "event" && e.body.eventType === "assistant.delta",
      )
      .map((e) => (e.body as { payload: { text: string } }).payload.text)
      .join("");
    assert.equal(text, "hello world");
    assert.equal(j.state(c.commandId), "terminal_pending");
    assert.equal(a.startCalls, 1);
  } finally {
    j.close();
    f.cleanup();
  }
});
for (const history of [
  { state: "unknown", sessionId: "session", turnId: null },
  {
    state: "completed",
    sessionId: "session",
    turnId: "turn",
    assistantText: "different",
  },
  {
    state: "completed",
    sessionId: "foreign",
    turnId: "turn",
    assistantText: "prefix",
  },
] as const)
  test(`unmatched history remains quarantined ${JSON.stringify(history)}`, async () => {
    const f = fixture();
    const j = new Journal(f.path, () => options);
    const c = command();
    const a = adapter();
    a.inspectTurn = async () => history;
    try {
      await j.execute(c, a);
      j.recordNative(c.commandId, {
        kind: "delta",
        sessionId: "session",
        turnId: "turn",
        text: "prefix",
      });
      await j.reconcile(c.commandId, a);
      assert.equal(j.state(c.commandId), "recovering");
      assert.equal(j.workspaceLocked(c.workspaceId), true);
    } finally {
      j.close();
      f.cleanup();
    }
  });
test("offline expiry blocks approval responses until verified renewal and never assumes child stopped", async () => {
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  let answers = 0;
  a.respondToRequest = async () => {
    answers++;
  };
  try {
    await j.execute(c, a);
    await j.watchLease(new Date(0).toISOString(), a, "session", "turn");
    assert.equal(j.state(c.commandId), "recovering");
    assert.equal(j.workspaceLocked(c.workspaceId), true);
    const reply: BridgeCommand = {
      ...c,
      commandId: randomUUID(),
      body: {
        kind: "respond",
        requestId: 1,
        response: { kind: "decision", allow: true },
        approvalRecordId: randomUUID(),
      },
    };
    await assert.rejects(j.execute(reply, a), /expired/);
    j.renewLease(c.deviceId, c.expiresAt, { [c.runId]: c.generation });
    await assert.rejects(j.execute(reply, a), /recovery/);
    assert.equal(answers, 0);
  } finally {
    j.close();
    f.cleanup();
  }
});
test("escaped Unicode output preserves all text within serialized durable cap", async () => {
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  try {
    await j.execute(c, a);
    const text = '\n\0\t"\\😀'.repeat(4000);
    j.recordNative(c.commandId, {
      kind: "delta",
      sessionId: "session",
      turnId: "turn",
      text,
    });
    const events = j.pending(100);
    for (const e of events)
      assert.ok(Buffer.byteLength(JSON.stringify(e)) <= 16384);
    assert.equal(
      events
        .filter((e) => e.body.kind === "event")
        .map((e) => (e.body as { payload: { text: string } }).payload.text)
        .join(""),
      text,
    );
  } finally {
    j.close();
    f.cleanup();
  }
});

// Controlled clock covers both expiry boundaries without a scheduler latency
// assumption. Removing watchLease's renewed-expiry check must fail before t=90.
test("watchdog fires offline automatically and verified renewal extends the lease", async (context) => {
  context.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 1800000000000 });
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const c = command();
  c.expiresAt = new Date(Date.now() + 25).toISOString();
  const a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  try {
    await j.execute(c, a);
    j.renewLease(c.deviceId, new Date(Date.now() + 90).toISOString(), {
      [c.runId]: c.generation,
    });
    context.mock.timers.tick(25); // Original lease expires; renewal must rearm it.
    await Promise.resolve();
    context.mock.timers.tick(64); // Still valid immediately before expiry, t=89.
    await Promise.resolve();
    assert.deepEqual(a.interrupts, []);
    context.mock.timers.tick(1); // Verified renewed lease expires at t=90.
    await Promise.resolve();
    assert.deepEqual(a.interrupts, ["session/turn"]);
    assert.equal(j.state(c.commandId), "recovering");
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    j.close();
    f.cleanup();
  }
});
test("completion beats watchdog but ownership waits for committed terminal acknowledgement", async () => {
  const f = fixture();
  const j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  try {
    await j.execute(c, a);
    j.recordNative(c.commandId, {
      kind: "terminal",
      sessionId: "session",
      turnId: "turn",
      status: "completed",
    });
    await j.watchLease(new Date(0).toISOString(), a, "session", "turn");
    assert.deepEqual(a.interrupts, []);
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("WSS reconnect sends header credentials and replays journal without a second native start", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  const f = fixture(),
    c = command(),
    a = adapter();
  const j = new Journal(f.path, () => options);
  a.inspectTurn = async () => ({
    state: "unknown",
    sessionId: "session",
    turnId: null,
  });
  a.events = async function* () {
    await new Promise<void>(() => {});
  };
  let headers: Record<string, string> = {};
  const uploaded: unknown[] = [];
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 1;
    constructor(url: string, init: { headers: Record<string, string> }) {
      super();
      headers = init.headers;
      assert.equal(url, "wss://example.test/api/v1/harness/connect");
      queueMicrotask(() => this.dispatchEvent(new Event("open")));
    }
    send(raw: string) {
      const value = JSON.parse(raw);
      if (value.poll) {
        queueMicrotask(() =>
          this.dispatchEvent(
            new MessageEvent("message", {
              data: JSON.stringify({
                commands: [c],
                lease: {
                  deviceId: c.deviceId,
                  expiresAt: c.expiresAt,
                  runs: { [c.runId]: c.generation },
                },
              }),
            }),
          ),
        );
        setTimeout(() => this.close(), 25);
      } else {
        assert.ok(j.pending().some((e) => e.sourceSeq === value.sourceSeq));
        uploaded.push(value);
        queueMicrotask(() =>
          this.dispatchEvent(
            new MessageEvent("message", {
              data: JSON.stringify({
                ack: {
                  runId: value.runId,
                  sourceId: value.sourceId,
                  sourceSeq: value.sourceSeq,
                  generation: value.generation,
                  canonicalSeq: value.sourceSeq,
                },
              }),
            }),
          ),
        );
      }
    }
    close() {
      if (this.readyState !== 3) {
        this.readyState = 3;
        this.dispatchEvent(new Event("close"));
      }
    }
  }
  const original = globalThis.WebSocket;
  globalThis.WebSocket = Socket as unknown as typeof WebSocket;
  t.after(() => {
    globalThis.WebSocket = original;
  });
  try {
    const args = {
      url: "wss://example.test/api/v1/harness/connect",
      deviceId: c.deviceId,
      credentials: { accessToken: "jwt", grantToken: "grant" },
      journal: j,
      adapterFor: () => a,
      signal: new AbortController().signal,
    };
    await connectBridge(args);
    await connectBridge(args);
    assert.equal(a.startCalls, 1);
    assert.equal(uploaded.length, 1);
    assert.deepEqual(j.pending(), []);
    assert.equal(headers.Authorization, "Bearer jwt");
    assert.equal(headers["X-NOUS-Integration-Grant"], "grant");
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("a narrow grant renewal cannot extend another run on the same device", async () => {
  const f = fixture(),
    j = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  const other = { ...command(), deviceId: c.deviceId };
  try {
    await j.execute(c, a);
    await j.execute(other, adapter());
    j.renewLease(c.deviceId, new Date(Date.now() + 60000).toISOString(), {
      [other.runId]: other.generation,
    });
    void j.watchLease(new Date(0).toISOString(), a, "session", "turn");
    await new Promise((r) => setTimeout(r, 5));
    assert.deepEqual(a.interrupts, ["session/turn"]);
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("renewal for a new generation cannot extend a fenced native turn", async () => {
  const f = fixture(),
    j = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  try {
    await j.execute(c, a);
    j.renewLease(c.deviceId, c.expiresAt, { [c.runId]: c.generation + 1 });
    void j.watchLease(new Date(0).toISOString(), a, "session", "turn");
    await new Promise((r) => setTimeout(r, 5));
    assert.deepEqual(a.interrupts, ["session/turn"]);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("failed targeted interrupt retries on redelivery and successful delivery stays deduped", async () => {
  const f = fixture();
  let j = new Journal(f.path, () => options);
  const c = command();
  const a = adapter();
  a.startTurn = async () => {
    a.startCalls++;
    return { id: "turn" };
  };
  a.interruptTurn = async (sessionId, turnId) => {
    a.interrupts.push(`${sessionId}/${turnId}`);
    if (a.interrupts.length === 1)
      throw new Error("transient interrupt delivery failure");
  };
  const stop: BridgeCommand = {
    ...c,
    commandId: randomUUID(),
    body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
  };
  try {
    await j.execute(c, a);
    await j.execute(stop, a);
    assert.equal(j.state(stop.commandId), "recovering");
    await assert.rejects(
      j.execute({ ...stop, expiresAt: new Date(0).toISOString() }, a),
      /lease expired/,
    );
    await assert.rejects(
      j.execute(
        {
          ...stop,
          body: { kind: "interrupt", sessionId: "session", turnId: "foreign" },
        },
        a,
      ),
      /payload conflict/,
    );
    assert.deepEqual(a.interrupts, ["session/turn"]);
    j.close();
    j = new Journal(f.path, () => options);
    await j.execute(stop, a);
    assert.deepEqual(a.interrupts, ["session/turn", "session/turn"]);
    assert.equal(j.state(stop.commandId), "delivered");
    assert.equal(j.workspaceLocked(c.workspaceId), true);
    j.close();
    j = new Journal(f.path, () => options);
    await j.execute(stop, a);
    assert.equal(a.interrupts.length, 2);
    assert.equal(a.startCalls, 1);
    j.recordNative(c.commandId, {
      kind: "terminal",
      sessionId: "session",
      turnId: "turn",
      status: "interrupted",
    });
    for (const value of j.pending()) {
      j.acknowledge({
        runId: value.runId,
        sourceId: value.sourceId,
        sourceSeq: value.sourceSeq,
        generation: value.generation,
        canonicalSeq: value.sourceSeq,
      });
    }
    assert.equal(j.workspaceLocked(c.workspaceId), false);
    assert.equal(j.state(c.commandId), "interrupted");
  } finally {
    j.close();
    f.cleanup();
  }
});

test("ambiguous non-interrupt response is not replayed", async () => {
  const f = fixture(),
    j = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  let responses = 0;
  a.respondToRequest = async () => {
    responses++;
    throw new Error("response delivery uncertain");
  };
  const response: BridgeCommand = {
    ...c,
    commandId: randomUUID(),
    body: {
      kind: "respond",
      requestId: 1,
      response: { kind: "decision", allow: true },
      approvalRecordId: randomUUID(),
    },
  };
  try {
    await j.execute(c, a);
    await j.execute(response, a);
    await j.execute(response, a);
    assert.equal(responses, 1);
    assert.equal(j.state(response.commandId), "recovering");
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    j.close();
    f.cleanup();
  }
});

function gate() {
  let release!: () => void;
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { wait, release };
}

test("concurrent interrupt redelivery waits for the first successful receipt", async () => {
  const f = fixture(),
    j = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  const firstEntered = gate(),
    firstRelease = gate(),
    secondRelease = gate();
  const stop: BridgeCommand = {
    ...c,
    commandId: randomUUID(),
    body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
  };
  let calls = 0;
  a.interruptTurn = async () => {
    calls++;
    if (calls === 1) {
      await firstRelease.wait;
      firstEntered.release();
    } else {
      await secondRelease.wait;
      throw new Error("late failure");
    }
  };
  try {
    await j.execute(c, a);
    const first = j.execute(stop, a);
    const second = j.execute(stop, a);
    await new Promise<void>((resolve) => setImmediate(resolve));
    const concurrentCalls = calls;
    firstRelease.release();
    await firstEntered.wait;
    await first;
    assert.equal(j.state(stop.commandId), "delivered");
    secondRelease.release();
    await second;
    assert.equal(j.state(stop.commandId), "delivered");
    await j.execute(stop, a);
    assert.equal(concurrentCalls, 1);
    assert.equal(calls, 1);
  } finally {
    firstRelease.release();
    secondRelease.release();
    j.close();
    f.cleanup();
  }
});

test("another journal cannot deliver an interrupt while its durable claim is held", async () => {
  const f = fixture(),
    first = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  const entered = gate(),
    release = gate();
  const stop: BridgeCommand = {
    ...c,
    commandId: randomUUID(),
    body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
  };
  let calls = 0;
  a.interruptTurn = async () => {
    calls++;
    entered.release();
    await release.wait;
  };
  let second: Journal | undefined;
  let pending: Promise<void> | undefined;
  try {
    await first.execute(c, a);
    pending = first.execute(stop, a);
    await entered.wait;
    second = new Journal(f.path, () => options);
    const duplicate = second.execute(stop, a);
    await new Promise<void>((resolve) => setImmediate(resolve));
    const overlappingCalls = calls;
    release.release();
    await Promise.all([pending, duplicate]);
    assert.equal(overlappingCalls, 1);
    assert.equal(first.state(stop.commandId), "delivered");
    assert.equal(second.state(stop.commandId), "delivered");
    await first.execute(stop, a);
    await second.execute(stop, a);
    assert.equal(calls, 1);
  } finally {
    release.release();
    await pending;
    second?.close();
    first.close();
    f.cleanup();
  }
});

test("failed interrupt releases its durable claim for another journal to retry", async () => {
  const f = fixture(),
    first = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  const entered = gate(),
    release = gate();
  const stop: BridgeCommand = {
    ...c,
    commandId: randomUUID(),
    body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
  };
  let calls = 0;
  a.interruptTurn = async () => {
    calls++;
    if (calls === 1) {
      entered.release();
      await release.wait;
      throw new Error("transient interrupt failure");
    }
  };
  let second: Journal | undefined;
  let pending: Promise<void> | undefined;
  try {
    await first.execute(c, a);
    pending = first.execute(stop, a);
    await entered.wait;
    second = new Journal(f.path, () => options);
    await second.execute(stop, a);
    assert.equal(calls, 1);
    release.release();
    await pending;
    assert.equal(second.state(stop.commandId), "recovering");
    await second.execute(stop, a);
    assert.equal(calls, 2);
    assert.equal(second.state(stop.commandId), "delivered");
    await first.execute(stop, a);
    await second.execute(stop, a);
    assert.equal(calls, 2);
  } finally {
    release.release();
    await pending;
    second?.close();
    first.close();
    f.cleanup();
  }
});

for (const uncertainExit of [false, true])
  test(`dead executor interrupt claim recovery preserves native exit uncertainty: ${uncertainExit}`, async () => {
    const f = fixture(),
      j = new Journal(f.path, () => options),
      c = command(),
      a = adapter();
    a.startTurn = async () => ({ id: "turn" });
    const stop: BridgeCommand = {
      ...c,
      commandId: randomUUID(),
      body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
    };
    try {
      await j.execute(c, a);
      const script = `
      import { Journal } from ${JSON.stringify(new URL("../src/journal.ts", import.meta.url).href)};
      import { NativeExitUnconfirmed } from ${JSON.stringify(new URL("../src/rpc.ts", import.meta.url).href)};
      const j = new Journal(${JSON.stringify(f.path)}, () => (${JSON.stringify(options)}));
      await j.execute(${JSON.stringify(stop)}, { interruptTurn: async () => {
        if (${uncertainExit}) throw new NativeExitUnconfirmed();
        process.exit(91);
      } });
      process.exit(91);
    `;
      const child = spawnSync(
        process.execPath,
        [
          "--experimental-sqlite",
          "--import",
          "tsx",
          "--input-type=module",
          "-e",
          script,
        ],
        { encoding: "utf8", timeout: 10_000 },
      );
      assert.equal(child.status, 91, child.stderr);
      assert.equal(
        j.state(stop.commandId),
        uncertainExit ? "recovering" : "intent",
      );
      await j.execute(stop, a);
      assert.deepEqual(a.interrupts, uncertainExit ? [] : ["session/turn"]);
      if (!uncertainExit) assert.equal(j.state(stop.commandId), "delivered");
      await j.execute(stop, a);
      assert.equal(a.interrupts.length, uncertainExit ? 0 : 1);
      assert.equal(j.workspaceLocked(c.workspaceId), true);
    } finally {
      j.close();
      f.cleanup();
    }
  });

test("unconfirmed native exit retains the durable interrupt claim across journals", async () => {
  const f = fixture(),
    j = new Journal(f.path, () => options),
    c = command(),
    a = adapter();
  a.startTurn = async () => ({ id: "turn" });
  let calls = 0;
  a.interruptTurn = async () => {
    calls++;
    throw new NativeExitUnconfirmed();
  };
  const stop: BridgeCommand = {
    ...c,
    commandId: randomUUID(),
    body: { kind: "interrupt", sessionId: "session", turnId: "turn" },
  };
  let other: Journal | undefined;
  try {
    await j.execute(c, a);
    await j.execute(stop, a);
    assert.equal(j.state(stop.commandId), "recovering");
    other = new Journal(f.path, () => options);
    await j.execute(stop, a);
    await other.execute(stop, a);
    assert.equal(calls, 1);
    assert.equal(j.workspaceLocked(c.workspaceId), true);
  } finally {
    other?.close();
    j.close();
    f.cleanup();
  }
});
