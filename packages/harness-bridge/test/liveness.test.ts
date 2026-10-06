import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Journal } from "../src/journal.ts";
import type { HarnessAdapter, SessionOptions } from "../src/contracts.ts";

// A backend restart can drop the TCP peer without a close frame. Undici's
// WebSocket then never fires close/error, and before the watchdog the bridge
// loop awaited connectBridge forever: alive, no sockets, no polls, no retry.

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
    signal: new AbortController().signal,
    timing,
  };
}

test("a silent peer trips the liveness watchdog and the socket is closed", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
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
    const started = Date.now();
    await assert.rejects(
      connectBridge(args(j, { pollMs: 10, livenessMs: 40 })),
      /bridge liveness timeout/,
    );
    assert.ok(Date.now() - started < 2000, "watchdog fired promptly");
    assert.ok(polls >= 1, "polled at least once before giving up");
    assert.equal(closed, true);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("a peer that answers polls keeps the connection alive", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  let polls = 0;
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 1;
    deviceId = "";
    constructor() {
      super();
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
      // Server goes away cleanly after a while; this must be the only exit.
      if (polls === 6) setTimeout(() => this.close(), 5);
    }
    close() {
      if (this.readyState !== 3) {
        this.readyState = 3;
        this.dispatchEvent(new Event("close"));
      }
    }
  }
  withSocket(t, Socket);
  const f = fixture();
  const j = new Journal(f.path, () => options);
  try {
    // livenessMs is shorter than the total run; answered polls must reset it.
    await connectBridge(args(j, { pollMs: 10, livenessMs: 35 }));
    assert.ok(polls >= 6, `expected steady polling, got ${polls}`);
  } finally {
    j.close();
    f.cleanup();
  }
});

test("a socket that never opens trips the connect timeout", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 0;
    send() {}
    close() {}
  }
  withSocket(t, Socket);
  const f = fixture();
  const j = new Journal(f.path, () => options);
  try {
    await assert.rejects(
      connectBridge(args(j, { pollMs: 10, livenessMs: 40, connectMs: 30 })),
      /bridge connect timeout/,
    );
  } finally {
    j.close();
    f.cleanup();
  }
});
