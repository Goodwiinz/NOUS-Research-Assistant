import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { setImmediate } from "node:timers/promises";
import { Journal } from "../src/journal.ts";
import type { HarnessAdapter, SessionOptions } from "../src/contracts.ts";

// A backend restart can drop the TCP peer without a close frame. Undici's
// WebSocket then never fires close/error, and before the watchdog the bridge
// loop awaited connectBridge forever: alive, no sockets, no polls, no retry.
// The independent abort deadline turns a removed guard into a bounded failure.
// Mutation targets: src/connection.ts:242 connectTimer, :273 lastFrameAt reset,
// and :251 liveness check. Command: pnpm --dir packages/harness-bridge exec node
// --experimental-sqlite --import tsx --test test/liveness.test.ts

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

function fixture() {
  const dir = mkdtempSync(join(tmpdir(), "nous-liveness-"));
  return {
    path: join(dir, "journal.sqlite"),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

function withSocket(t: { after: (fn: () => void) => void }, Socket: unknown) {
  const original = globalThis.WebSocket;
  globalThis.WebSocket = Socket as unknown as typeof WebSocket;
  t.after(() => {
    globalThis.WebSocket = original;
  });
}

function args(j: Journal, timing: Record<string, number>) {
  return {
    url: "wss://example.test/api/v1/harness/connect",
    deviceId: randomUUID(),
    credentials: { accessToken: "jwt", grantToken: "grant" },
    journal: j,
    adapterFor: () => ({}) as unknown as HarnessAdapter,
    signal: AbortSignal.timeout(2000),
    timing,
  };
}

test("a silent peer trips the liveness watchdog and the socket is closed", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  t.mock.timers.enable({
    apis: ["Date", "setTimeout", "setInterval"],
    now: 1800000000000,
  });
  let polls = 0;
  let closed = false;
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      super();
      queueMicrotask(() => this.dispatchEvent(new Event("open")));
    }
    send(raw: string) {
      if (JSON.parse(raw).poll) polls += 1;
      // Never answer: half-open peer.
    }
    close() {
      closed = true;
    }
  }
  withSocket(t, Socket);
  const f = fixture();
  const j = new Journal(f.path, () => options);
  try {
    const failure = assert.rejects(
      connectBridge(args(j, { pollMs: 10, livenessMs: 40 })),
      /bridge liveness timeout/,
    );
    await setImmediate(); // Deliver open and its initial poll.
    for (let i = 0; i < 4; i++) {
      t.mock.timers.tick(10);
      await setImmediate();
    }
    assert.equal(closed, false, "the peer remains connected at the deadline");
    assert.equal(polls, 5, "initial poll plus four scheduled polls");
    t.mock.timers.tick(10);
    await failure;
    assert.equal(closed, true);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("a peer that answers polls keeps the connection alive", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  t.mock.timers.enable({
    apis: ["Date", "setTimeout", "setInterval"],
    now: 1800000000000,
  });
  let polls = 0;
  let peer!: Socket;
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 1;
    deviceId = "";
    constructor() {
      super();
      peer = this;
      queueMicrotask(() => this.dispatchEvent(new Event("open")));
    }
    send(raw: string) {
      const value = JSON.parse(raw);
      if (!value.poll) return;
      polls += 1;
      const reply = {
        commands: [],
        lease: {
          deviceId: value.deviceId,
          expiresAt: new Date(Date.now() + 60_000).toISOString(),
          runs: {},
        },
      };
      queueMicrotask(() =>
        this.dispatchEvent(
          new MessageEvent("message", { data: JSON.stringify(reply) }),
        ),
      );
    }
    close() {
      if (this.readyState !== 3) {
        this.readyState = 3;
        queueMicrotask(() => this.dispatchEvent(new Event("close")));
      }
    }
  }
  withSocket(t, Socket);
  const f = fixture();
  const j = new Journal(f.path, () => options);
  try {
    // Advance one poll at a time so queued replies arrive before the next tick.
    // Elapsed time exceeds livenessMs; answered polls must reset the deadline.
    let finished = false;
    const connected = connectBridge(args(j, { pollMs: 10, livenessMs: 35 }));
    const completion = assert.doesNotReject(connected);
    void connected.then(
      () => {
        finished = true;
      },
      () => {
        finished = true;
      },
    );
    await setImmediate();
    for (let i = 0; i < 5; i++) {
      t.mock.timers.tick(10);
      await setImmediate();
    }
    assert.equal(polls, 6);
    assert.equal(finished, false, "healthy replies keep the connection open");
    peer.close();
    await completion;
    assert.equal(finished, true, "only the peer close finishes the connection");
  } finally {
    j.close();
    f.cleanup();
  }
});

test("a socket that never opens trips the connect timeout", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  t.mock.timers.enable({
    apis: ["Date", "setTimeout", "setInterval"],
    now: 1800000000000,
  });
  let closed = false;
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 0;
    send() {}
    close() {
      closed = true;
    }
  }
  withSocket(t, Socket);
  const f = fixture();
  const j = new Journal(f.path, () => options);
  try {
    const failure = assert.rejects(
      connectBridge(args(j, { pollMs: 10, livenessMs: 40, connectMs: 30 })),
      /bridge connect timeout/,
    );
    t.mock.timers.tick(29);
    await setImmediate();
    assert.equal(closed, false);
    t.mock.timers.tick(1);
    await failure;
    assert.equal(closed, true);
  } finally {
    j.close();
    f.cleanup();
  }
});
