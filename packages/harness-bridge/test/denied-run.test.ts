import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Journal } from "../src/journal.ts";
import { reconcileLocal } from "../src/cli.ts";
import type { BridgeCommand, Rejection } from "../src/connection.ts";
import type { HarnessAdapter, SessionOptions } from "../src/contracts.ts";

// BR-1: NOUS refuses one run's events (its chat was deleted or linked to another
// project, its folder binding removed) while the device grant stays valid. The
// socket answers {"reject": ...} instead of closing with 4403. The journal must
// drop that run's unsent events and stop journaling for it, or the run is
// re-sent first on every reconnect and starves the device's other runs.
// Mutation targets: src/journal.ts reject() (events acked=2), the 'denied'
// guards in quarantine()/append()/observe()/reconcile(), and the reject branch
// in src/connection.ts. Command:
// pnpm --filter @nous/harness-bridge test test/denied-run.test.ts

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
const later = (ms = 60_000) => new Date(Date.now() + ms).toISOString();

function fixture() {
  const dir = mkdtempSync(join(tmpdir(), "nous-denied-"));
  return {
    path: join(dir, "journal.sqlite"),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

function start(deviceId: string, expiresAt = later()): BridgeCommand {
  return {
    deviceId,
    runId: randomUUID(),
    commandId: randomUUID(),
    workspaceId: randomUUID(),
    generation: 1,
    expiresAt,
    body: { kind: "start", input: "work" },
  };
}

/** Codex double: turns start, and history reports the turn as still running.
 * Like src/adapters/codex.ts, an instance interrupts only a turn it started or
 * read back with inspectTurn, so a fresh one (after a restart) knows none. */
function codex(name: string) {
  const known = new Set<string>();
  const value = {
    interrupts: 0, // interrupts Codex accepted
    unknownInterrupts: 0, // interrupts refused: "unknown active turn"
    async startSession() {
      return { id: `s-${name}` };
    },
    async resumeSession(id: string) {
      return { id };
    },
    async startTurn(sessionId: string) {
      known.add(`${sessionId}/t-${name}`);
      return { id: `t-${name}` };
    },
    async interruptTurn(sessionId: string, turnId: string) {
      if (!known.has(`${sessionId}/${turnId}`)) {
        value.unknownInterrupts += 1;
        throw new Error("unknown active turn");
      }
      value.interrupts += 1;
    },
    async respondToRequest() {},
    async inspectTurn(sessionId: string) {
      known.add(`${sessionId}/t-${name}`);
      return { state: "running", sessionId, turnId: `t-${name}` };
    },
    async closeSession() {},
    async *events() {},
  };
  return value as unknown as HarnessAdapter & {
    interrupts: number;
    unknownInterrupts: number;
  };
}

const refusal = (c: BridgeCommand, sourceSeq = 1): Rejection => ({
  runId: c.runId,
  sourceId: c.commandId,
  sourceSeq,
  generation: c.generation,
  code: "run_access_denied",
});
const queued = (j: Journal) =>
  j.pending(100).map((e) => `${e.runId}#${e.sourceSeq}`);

test("a refused run's unsent events are dropped and nothing more is journaled for it, across restarts", async () => {
  const f = fixture();
  const device = randomUUID();
  const r1 = start(device);
  const r2 = start(device);
  const a1 = codex("r1");
  const a2 = codex("r2");
  let j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, a1); // r1#1 observation:running
    j.recordNative(r1.commandId, { kind: "delta", sessionId: "s-r1", turnId: "t-r1", text: "after its chat was deleted" }); // r1#2
    await j.execute(r2, a2); // r2#1
    j.recordNative(r2.commandId, { kind: "delta", sessionId: "s-r2", turnId: "t-r2", text: "answer" }); // r2#2
    assert.throws(() => j.reject({ ...refusal(r1), runId: r2.runId }), /rejection identity mismatch/);
    assert.throws(() => j.reject({ ...refusal(r1), generation: 2 }), /rejection identity mismatch/);
    assert.throws(
      () => j.reject({ ...refusal(r1), code: "access_denied" } as unknown as Rejection),
      /rejection identity mismatch/,
    );
    assert.throws(() => j.reject({ ...refusal(r1), sourceSeq: 0 }), /rejection identity mismatch/);
    assert.throws(() => j.reject({ ...refusal(r1), sourceSeq: 3 }), /rejection identity mismatch/);
    assert.throws(() => j.reject({ ...refusal(r1), sourceId: randomUUID() }), /unknown rejection/);
    assert.equal(j.reject(refusal(r1)), true);
    // Another in-flight event of the same run is refused too: a no-op.
    assert.equal(j.reject(refusal(r1, 2)), false);
    assert.equal(j.state(r1.commandId), "denied");
    const onlyR2 = [`${r2.runId}#1`, `${r2.runId}#2`];
    assert.deepEqual(queued(j), onlyR2);
    // Native output, the watchdog's quarantine and reconnect reconciliation add
    // nothing for the refused run (today reconcile appends "running" each time).
    j.recordNative(r1.commandId, { kind: "delta", sessionId: "s-r1", turnId: "t-r1", text: "more" });
    j.recordNative(r1.commandId, { kind: "terminal", status: "interrupted", sessionId: "s-r1", turnId: "t-r1" });
    j.quarantine(r1.commandId);
    await j.reconcile(r1.commandId, a1);
    assert.deepEqual(queued(j), onlyR2);
    assert.equal(j.state(r1.commandId), "denied");
    // NOUS has no terminal evidence and keeps its workspace lock; so does the
    // bridge. The terminal Codex reported after the reject was not journaled,
    // so it does not release the folder either (D-2', next test).
    assert.equal(j.workspaceLocked(r1.workspaceId), true);
    j.close();
    j = new Journal(f.path, () => options);
    assert.equal(j.state(r1.commandId), "denied");
    await j.reconcile(r1.commandId, a1);
    assert.deepEqual(queued(j), onlyR2);
  } finally {
    j.close();
    f.cleanup();
  }
});

// D-2' (plan amendment from the D1 review). backend/src/api/harness.py
// authorizes an event before it looks up the event's receipt, so a reject can
// also answer the replay of an event NOUS already stored, and NOUS may then
// finalize the run and release its own folder lock. The bridge's lock follows
// its own evidence instead:
// - a terminal observation is already journaled: Codex has stopped writing, so
//   the bridge releases the folder (safe whatever NOUS did), and a start NOUS
//   sends there is not refused as "workspace quarantined", which closes the
//   socket;
// - none is journaled (mid-run): the bridge keeps the folder, as in D-2. The
//   lease watchdog interrupts the turn, and the folder stays reserved until
//   disconnect, connect and workspace add.
// Mutations in src/journal.ts reject(): delete the lock release, and "after its
// local terminal observation" fails on workspaceLocked (true !== false);
// release the lock unconditionally, and "mid-run" fails on workspaceLocked
// (false !== true), as does the test above.
for (const [when, finished] of [
  ["mid-run", false],
  ["after its local terminal observation", true],
] as const)
  test(`a run refused ${when} ${finished ? "releases" : "keeps"} its folder`, async () => {
    const f = fixture();
    const r1 = start(randomUUID());
    let j = new Journal(f.path, () => options);
    try {
      await j.execute(r1, codex("r1")); // r1#1 observation:running
      if (finished)
        j.recordNative(r1.commandId, { kind: "terminal", status: "completed", sessionId: "s-r1", turnId: "t-r1" }); // r1#2, unacknowledged
      // NOUS refuses r1#1: a first send, or the replay of one whose ack was lost.
      assert.equal(j.reject(refusal(r1)), true);
      assert.equal(j.state(r1.commandId), "denied");
      assert.deepEqual(queued(j), []);
      assert.equal(j.workspaceLocked(r1.workspaceId), !finished);
      // Only a released folder takes the next run NOUS sends to it.
      const next = { ...start(r1.deviceId), workspaceId: r1.workspaceId };
      if (finished) {
        await j.execute(next, codex("next"));
        assert.equal(j.state(next.commandId), "running");
      } else
        await assert.rejects(j.execute(next, codex("next")), /workspace quarantined/);
      j.close();
      j = new Journal(f.path, () => options);
      assert.equal(j.state(r1.commandId), "denied");
      assert.deepEqual(
        j.activeCommands().map((c) => c.commandId),
        [finished ? next.commandId : r1.commandId],
      );
    } finally {
      j.close();
      f.cleanup();
    }
  });

// A reject names an event, not a run state. Codex can finish the turn while the
// user's approval is being delivered, so the respond's command_ack is journaled
// after the terminal observation. Once NOUS acknowledges that observation the
// run is completed; a reject of the later command_ack must drop it and leave
// the run completed, not flip it to 'denied'.
// Mutation: delete the terminal() guard in src/journal.ts reject() and the
// reject returns true (true !== false).
test("a reject that names an event journaled after the run completed keeps it completed", async () => {
  const f = fixture();
  const r1 = start(randomUUID());
  const a1 = codex("r1");
  const j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, a1); // r1#1 observation:running
    a1.respondToRequest = async () => {
      j.recordNative(r1.commandId, { kind: "terminal", status: "completed", sessionId: "s-r1", turnId: "t-r1" }); // r1#2
    };
    const respond: BridgeCommand = {
      ...r1,
      commandId: randomUUID(),
      body: { kind: "respond", requestId: 7, approvalRecordId: randomUUID(), response: { kind: "decision", allow: true } },
    };
    await j.execute(respond, a1); // r1#3 command_ack
    for (const sourceSeq of [1, 2])
      j.acknowledge({ runId: r1.runId, sourceId: r1.commandId, sourceSeq, generation: 1, canonicalSeq: sourceSeq });
    assert.equal(j.state(r1.commandId), "completed");
    assert.deepEqual(queued(j), [`${r1.runId}#3`]);
    assert.equal(j.reject(refusal(r1, 3)), false);
    assert.equal(j.state(r1.commandId), "completed");
    assert.deepEqual(queued(j), []);
    assert.equal(j.workspaceLocked(r1.workspaceId), false);
  } finally {
    j.close();
    f.cleanup();
  }
});

// Renewal gap: after a grant renewal NOUS can refuse a run's event before the
// next lease poll re-leases the run under the new grant, then keep leasing it.
// A reject is final for the run, so the bridge must stop extending its lease or
// the watchdog re-arms on every renewal and never interrupts the turn.
// Mutation: drop the 'denied' skip in src/journal.ts renewLease() and the turn
// is not interrupted (0 !== 1).
test("a refused run's lease is not renewed, so its turn is interrupted even while NOUS still leases it", async () => {
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  const a1 = codex("r1");
  const j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, a1); // arms the watchdog for the command's lease
    assert.equal(j.reject(refusal(r1)), true);
    j.renewLease(r1.deviceId, later(60_000), { [r1.runId]: r1.generation });
    await new Promise((resolve) => setTimeout(resolve, 250)); // the lease lapses
    assert.equal(a1.interrupts, 1);
    assert.equal(j.state(r1.commandId), "denied");
  } finally {
    j.close();
    f.cleanup();
  }
});

// D-2' released the folder of a run refused after its terminal observation was
// journaled: Codex has stopped, so the watchdog has no turn to interrupt.
// Mutation: drop the 'denied'-without-lock return in src/journal.ts watchLease()
// and the finished turn is interrupted (1 !== 0).
test("the watchdog does not interrupt a refused run whose folder was released", async () => {
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  const a1 = codex("r1");
  const j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, a1); // r1#1 observation:running
    j.recordNative(r1.commandId, { kind: "terminal", status: "completed", sessionId: "s-r1", turnId: "t-r1" }); // r1#2
    assert.equal(j.reject(refusal(r1)), true);
    assert.equal(j.workspaceLocked(r1.workspaceId), false);
    await new Promise((resolve) => setTimeout(resolve, 250)); // the lease lapses
    assert.equal(a1.interrupts, 0);
    assert.equal(j.state(r1.commandId), "denied");
  } finally {
    j.close();
    f.cleanup();
  }
});

// After a restart the in-process watchdog timer is gone and adapterFor builds a
// fresh Codex adapter, which interrupts only a turn it started or read back.
// Reconcile reads the refused run's turn back once, reports nothing, and arms
// the watchdog; later passes neither read it again nor re-arm.
// Mutations in src/journal.ts reconcile(): drop the inspectTurn call and the
// interrupt is refused (0 !== 1); drop the armed/claimed early return and the
// second pass reads the turn again (2 !== 1).
test("after a restart a refused run's turn is read back once and interrupted once its lease lapses", async () => {
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  let j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, codex("r1"));
    j.reject(refusal(r1));
    j.close(); // a restart drops the in-process watchdog timer and the adapter
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    const inspect = a1.inspectTurn;
    let inspected = 0;
    a1.inspectTurn = (sessionId, commandId) => {
      inspected += 1;
      return inspect(sessionId, commandId);
    };
    await new Promise((resolve) => setTimeout(resolve, 250)); // the lease lapses
    await j.reconcile(r1.commandId, a1);
    await j.reconcile(r1.commandId, a1); // the next pass, e.g. the next socket open
    assert.equal(a1.interrupts, 1);
    assert.equal(inspected, 1);
    assert.deepEqual(j.pending(), []);
    assert.equal(j.state(r1.commandId), "denied");
  } finally {
    j.close();
    f.cleanup();
  }
});

// connection.ts reconciles every locked run on each socket open, so a thread
// Codex no longer knows must not fail the reconnect. Mutation: let the
// inspectTurn error escape reconcile() and it rejects.
test("a refused run whose thread Codex no longer knows does not fail the reconnect", async () => {
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  let j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, codex("r1"));
    j.reject(refusal(r1));
    j.close();
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    a1.inspectTurn = async () => {
      throw new Error("Codex no longer knows this thread");
    };
    await new Promise((resolve) => setTimeout(resolve, 250)); // the lease lapses
    await j.reconcile(r1.commandId, a1);
    // The watchdog still fires; Codex has no such turn to stop.
    assert.equal(a1.unknownInterrupts, 1);
    assert.deepEqual(j.pending(), []);
    assert.equal(j.state(r1.commandId), "denied");
  } finally {
    j.close();
    f.cleanup();
  }
});

// runBridge (src/cli.ts) runs reconcileLocal before every connection attempt so
// the expiry watchdog works while the network is down. After a restart it must
// re-arm a refused run's watchdog too, not only a 'recovering' run's.
// Mutations: reconcile only 'recovering' runs in reconcileLocal(), or drop the
// inspectTurn call in src/journal.ts reconcile(), and the turn is not
// interrupted (0 !== 1).
test("offline after a restart, the local pass re-arms a refused run's watchdog", async () => {
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  let j = new Journal(f.path, () => options);
  try {
    await j.execute(r1, codex("r1"));
    j.reject(refusal(r1));
    j.close(); // a restart drops the in-process watchdog timer and the adapter
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    await reconcileLocal(j, () => a1);
    await new Promise((resolve) => setTimeout(resolve, 250)); // the lease lapses
    assert.equal(a1.interrupts, 1);
    assert.equal(j.state(r1.commandId), "denied");
  } finally {
    j.close();
    f.cleanup();
  }
});

function withSocket(t: { after: (fn: () => void) => void }, Socket: unknown) {
  const original = globalThis.WebSocket;
  globalThis.WebSocket = Socket as unknown as typeof WebSocket;
  t.after(() => {
    globalThis.WebSocket = original;
  });
}

// On socket open connection.ts reconciles every locked run, but a turn Codex
// cannot vouch for yet (history "unknown") is only quarantined there, with no
// watchdog. If NOUS refuses that run later on the same socket, a lease frame
// must arm its watchdog, as it reconciles a 'recovering' run, and only once.
// Mutations: reconcile only 'recovering' runs per lease frame in
// src/connection.ts and the turn is not interrupted (0 !== 1); drop the
// armed/claimed early return in src/journal.ts reconcile() and every later
// frame reads the turn again.
test("a run refused while connected gets its watchdog from the next lease frame", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  let j = new Journal(f.path, () => options);
  let polls = 0;
  let refused = false;
  let inspected = 0;
  let inspectedBeforeReject = -1;
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      super();
      queueMicrotask(() => this.dispatchEvent(new Event("open")));
    }
    send(raw: string) {
      if (!JSON.parse(raw).poll) return; // events stay unacknowledged
      // Stands in for a reject frame (Task D5) after the first lease frame.
      if ((polls += 1) === 2) {
        inspectedBeforeReject = inspected;
        refused = j.reject(refusal(r1));
      }
      const lease = { deviceId: r1.deviceId, expiresAt: later(), runs: {} };
      queueMicrotask(() =>
        this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ commands: [], lease }) })),
      );
    }
    close() {
      if (this.readyState !== 3) {
        this.readyState = 3;
        // Asynchronous, as a real WebSocket: the bridge's own error surfaces.
        queueMicrotask(() => this.dispatchEvent(new Event("close")));
      }
    }
  }
  withSocket(t, Socket);
  try {
    await j.execute(r1, codex("r1"));
    j.close(); // a restart drops the watchdog timer; r1 reopens 'recovering'
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    const inspect = a1.inspectTurn;
    a1.inspectTurn = async (sessionId, commandId) => {
      inspected += 1;
      if (!refused) return { state: "unknown", sessionId, turnId: null };
      return inspect(sessionId, commandId);
    };
    await new Promise((resolve) => setTimeout(resolve, 250)); // the lease lapses
    await connectBridge({
      url: "wss://example.test/api/v1/harness/connect",
      deviceId: r1.deviceId,
      credentials: { accessToken: "jwt", grantToken: "grant" },
      journal: j,
      adapterFor: () => a1,
      signal: AbortSignal.timeout(200),
      timing: { pollMs: 10, livenessMs: 1000 },
    });
    assert.ok(polls > 3);
    assert.equal(j.state(r1.commandId), "denied");
    assert.equal(a1.interrupts, 1);
    assert.ok(inspectedBeforeReject > 0);
    assert.equal(inspected, inspectedBeforeReject + 1);
  } finally {
    j.close();
    f.cleanup();
  }
});

// Renewal gap: NOUS can keep leasing a run the bridge refused, and the bridge
// lets the run's local lease lapse (renewLease skips it), so the watchdog has
// blocked it. NOUS redelivers the user's Stop and any approval on every poll.
// An interrupt only stops work, so it needs no lease; an approval for a refused
// run is dropped. Neither may close the socket ("lease expired" / "approval
// blocked during recovery" used to, on every redelivery).
// Mutations in src/journal.ts executeCommand(): drop the refused-run lease
// exemption, or the respond drop, and connectBridge rejects.
test("NOUS still leasing a refused run: its Stop is delivered, its approval dropped, and the socket stays open", async (t) => {
  const { connectBridge } = await import("../src/connection.ts");
  const f = fixture();
  const r1 = start(randomUUID(), later(150));
  const a1 = codex("r1");
  let responded = 0;
  a1.respondToRequest = async () => {
    responded += 1;
  };
  const stop: BridgeCommand = {
    ...r1,
    commandId: randomUUID(),
    expiresAt: later(),
    body: { kind: "interrupt", sessionId: "s-r1", turnId: "t-r1" },
  };
  const approve: BridgeCommand = {
    ...r1,
    commandId: randomUUID(),
    expiresAt: later(),
    body: { kind: "respond", requestId: 7, approvalRecordId: randomUUID(), response: { kind: "decision", allow: true } },
  };
  const j = new Journal(f.path, () => options);
  let polls = 0;
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      super();
      queueMicrotask(() => this.dispatchEvent(new Event("open")));
    }
    send(raw: string) {
      if (!JSON.parse(raw).poll) return;
      polls += 1;
      const lease = { deviceId: r1.deviceId, expiresAt: later(), runs: { [r1.runId]: r1.generation } };
      queueMicrotask(() =>
        this.dispatchEvent(
          new MessageEvent("message", { data: JSON.stringify({ commands: [stop, approve], lease }) }),
        ),
      );
    }
    close() {
      if (this.readyState !== 3) {
        this.readyState = 3;
        // Asynchronous, as a real WebSocket: the bridge's own error surfaces.
        queueMicrotask(() => this.dispatchEvent(new Event("close")));
      }
    }
  }
  withSocket(t, Socket);
  try {
    await j.execute(r1, a1);
    assert.equal(j.reject(refusal(r1)), true);
    await new Promise((resolve) => setTimeout(resolve, 250)); // the watchdog blocks the lapsed lease
    assert.equal(a1.interrupts, 1);
    await connectBridge({
      url: "wss://example.test/api/v1/harness/connect",
      deviceId: r1.deviceId,
      credentials: { accessToken: "jwt", grantToken: "grant" },
      journal: j,
      adapterFor: () => a1,
      signal: AbortSignal.timeout(150),
      timing: { pollMs: 10, livenessMs: 1000 },
    });
    assert.ok(polls > 2);
    assert.equal(j.state(stop.commandId), "delivered");
    assert.equal(a1.interrupts, 2); // once, despite every redelivery
    assert.equal(j.state(approve.commandId), "missing");
    assert.equal(responded, 0);
    assert.equal(j.state(r1.commandId), "denied");
  } finally {
    j.close();
    f.cleanup();
  }
});
