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

// Watchdog tests give a start a short lease. execute() refuses a lapsed lease
// ("lease expired; verified renewal required") and arms the watchdog for what
// is left of it, so the lease is stamped right before execute, never when the
// test began: under full-suite load, opening the journal alone can outlast it.
// The command's identity digest excludes expiresAt.
const LEASE_MS = 300;
async function executeLeased(j: Journal, c: BridgeCommand, adapter: HarnessAdapter): Promise<void> {
  c.expiresAt = later(LEASE_MS);
  await j.execute(c, adapter);
}
/** Outlasts a lease that executeLeased() stamped before this call. */
const lapse = () => new Promise((resolve) => setTimeout(resolve, LEASE_MS + 100));
// Connection tests stop after a number of polls, not a fixed time, so a slow
// run still serves every frame its assertions count on; the timeout only ends
// a connection that stalls, whose assertions then fail.
const until = (stop: AbortController) =>
  AbortSignal.any([stop.signal, AbortSignal.timeout(5000)]);

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
  const r1 = start(randomUUID());
  const a1 = codex("r1");
  const j = new Journal(f.path, () => options);
  try {
    await executeLeased(j, r1, a1); // arms the watchdog for the command's lease
    assert.equal(j.reject(refusal(r1)), true);
    j.renewLease(r1.deviceId, later(60_000), { [r1.runId]: r1.generation });
    await lapse(); // the lease lapses
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
  const r1 = start(randomUUID());
  const a1 = codex("r1");
  const j = new Journal(f.path, () => options);
  try {
    await executeLeased(j, r1, a1); // r1#1 observation:running
    j.recordNative(r1.commandId, { kind: "terminal", status: "completed", sessionId: "s-r1", turnId: "t-r1" }); // r1#2
    assert.equal(j.reject(refusal(r1)), true);
    assert.equal(j.workspaceLocked(r1.workspaceId), false);
    await lapse(); // the lease lapses
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
  const r1 = start(randomUUID());
  let j = new Journal(f.path, () => options);
  try {
    await executeLeased(j, r1, codex("r1"));
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
    await lapse(); // the lease lapses
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
  const r1 = start(randomUUID());
  let j = new Journal(f.path, () => options);
  try {
    await executeLeased(j, r1, codex("r1"));
    j.reject(refusal(r1));
    j.close();
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    a1.inspectTurn = async () => {
      throw new Error("Codex no longer knows this thread");
    };
    await lapse(); // the lease lapses
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
  const r1 = start(randomUUID());
  let j = new Journal(f.path, () => options);
  try {
    await executeLeased(j, r1, codex("r1"));
    j.reject(refusal(r1));
    j.close(); // a restart drops the in-process watchdog timer and the adapter
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    await reconcileLocal(j, () => a1);
    await lapse(); // the lease lapses
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
  const r1 = start(randomUUID());
  let j = new Journal(f.path, () => options);
  const done = new AbortController();
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
      if (polls === 8) return done.abort(); // six lease frames after the refusal
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
    await executeLeased(j, r1, codex("r1"));
    j.close(); // a restart drops the watchdog timer; r1 reopens 'recovering'
    j = new Journal(f.path, () => options);
    const a1 = codex("r1");
    const inspect = a1.inspectTurn;
    a1.inspectTurn = async (sessionId, commandId) => {
      inspected += 1;
      if (!refused) return { state: "unknown", sessionId, turnId: null };
      return inspect(sessionId, commandId);
    };
    await lapse(); // the lease lapses
    await connectBridge({
      url: "wss://example.test/api/v1/harness/connect",
      deviceId: r1.deviceId,
      credentials: { accessToken: "jwt", grantToken: "grant" },
      journal: j,
      adapterFor: () => a1,
      signal: until(done),
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
  const r1 = start(randomUUID());
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
  const done = new AbortController();
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
      if ((polls += 1) === 6) return done.abort(); // five redeliveries
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
    await executeLeased(j, r1, a1);
    assert.equal(j.reject(refusal(r1)), true);
    await lapse(); // the watchdog blocks the lapsed lease
    assert.equal(a1.interrupts, 1);
    await connectBridge({
      url: "wss://example.test/api/v1/harness/connect",
      deviceId: r1.deviceId,
      credentials: { accessToken: "jwt", grantToken: "grant" },
      journal: j,
      adapterFor: () => a1,
      signal: until(done),
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

// The reject frame itself (backend/src/api/harness.py sends it per refused
// event, in place of the ack). Before it, connection.ts read it as an invalid
// lease frame, closed the socket and reconnected into the same refused run.
// The notice names the folder as journal.reject() left it: reserved mid-run
// (D-2), released by the bridge once the run's terminal observation was
// journaled (D-2'); NOUS normally keeps its own lock, so that notice says what
// to do if NOUS still reports the folder busy. The run's other in-flight
// events are refused too; they log nothing more.
// Mutations in src/connection.ts: remove the reject branch and connectBridge
// rejects with "invalid lease response"; always print the reserved notice and
// "after its local terminal observation" fails; log whatever journal.reject()
// returns and both variants log more than once.
for (const [when, finished] of [
  ["mid-run", false],
  ["after its local terminal observation", true],
] as const)
  test(`a reject frame keeps the socket: the device's other runs drain and the refused run is never re-sent (${when})`, async (t) => {
    const { connectBridge } = await import("../src/connection.ts");
    const logged = t.mock.method(console, "error", () => {});
    const f = fixture();
    const device = randomUUID();
    const r1 = start(device);
    const r2 = start(device);
    const adapters: Record<string, HarnessAdapter> = {
      [r1.runId]: codex("r1"),
      [r2.runId]: codex("r2"),
    };
    const j = new Journal(f.path, () => options);
    const received: string[] = [];
    let stop = new AbortController();
    let canonical = 1;
    let polls = 0;
    let stopAtPoll = 0; // 0: stop on r2's completion instead
    class Socket extends EventTarget {
      static OPEN = 1;
      readyState = 1;
      constructor() {
        super();
        queueMicrotask(() => this.dispatchEvent(new Event("open")));
      }
      reply(value: unknown) {
        queueMicrotask(() =>
          this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) })),
        );
      }
      send(raw: string) {
        const value = JSON.parse(raw);
        if (value.poll) {
          if ((polls += 1) === stopAtPoll) return stop.abort();
          this.reply({ commands: [], lease: { deviceId: device, expiresAt: later(), runs: { [r2.runId]: 1 } } });
          return;
        }
        received.push(`${value.runId}#${value.sourceSeq}`);
        const receipt = { runId: value.runId, sourceId: value.sourceId, sourceSeq: value.sourceSeq, generation: value.generation };
        // backend/src/api/harness.py: r1's chat is gone, the device grant is fine.
        if (value.runId === r1.runId) {
          this.reply({ reject: { ...receipt, code: "run_access_denied" } });
          return;
        }
        this.reply({ ack: { ...receipt, canonicalSeq: (canonical += 1) } });
        if (value.body.kind === "observation" && value.body.state === "completed")
          setTimeout(() => stop.abort(), 20);
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
    const connect = () =>
      connectBridge({
        url: "wss://example.test/api/v1/harness/connect",
        deviceId: device,
        credentials: { accessToken: "jwt", grantToken: "grant" },
        journal: j,
        adapterFor: (_workspaceId, runId) => adapters[runId]!,
        signal: until(stop),
        timing: { pollMs: 10, livenessMs: 1000 },
      });
    try {
      await j.execute(r1, adapters[r1.runId]!);
      j.recordNative(r1.commandId, { kind: "delta", sessionId: "s-r1", turnId: "t-r1", text: "after its chat was deleted" });
      if (finished)
        j.recordNative(r1.commandId, { kind: "terminal", status: "completed", sessionId: "s-r1", turnId: "t-r1" });
      await j.execute(r2, adapters[r2.runId]!);
      j.recordNative(r2.commandId, { kind: "delta", sessionId: "s-r2", turnId: "t-r2", text: "answer" });
      j.recordNative(r2.commandId, { kind: "terminal", status: "completed", sessionId: "s-r2", turnId: "t-r2" });
      await connect();
      assert.equal(j.state(r2.commandId), "completed");
      assert.equal(j.workspaceLocked(r2.workspaceId), false);
      assert.equal(j.state(r1.commandId), "denied");
      assert.equal(j.workspaceLocked(r1.workspaceId), !finished);
      assert.deepEqual(j.pending(), []);
      // r1#2 was in flight with r1#1: NOUS refused it too, and the bridge
      // printed one notice for the run.
      assert.ok(received.includes(`${r1.runId}#2`));
      const notice = finished
        ? `NOUS refused run ${r1.runId}: its output is no longer uploaded. The run had already finished on this device, so the bridge released its folder, but if NOUS still reports the folder busy, run disconnect, connect and workspace add again.`
        : `NOUS refused run ${r1.runId}: its output is no longer uploaded, and its folder stays reserved until you run disconnect, connect and workspace add again.`;
      assert.deepEqual(logged.mock.calls.map((c) => c.arguments), [[notice]]);
      // Grant renewal forces a reconnect at least every 15 minutes: nothing of r1
      // is sent again and no new "running" observation is journaled for it.
      // Stop at the reconnect's third poll, not after a fixed time, so even a
      // slow run has passed its open and two lease frames, each of which sends
      // whatever is pending.
      const before = received.length;
      stop = new AbortController();
      stopAtPoll = polls + 3;
      await connect();
      assert.equal(polls, stopAtPoll);
      assert.deepEqual(received.slice(before), []);
      assert.deepEqual(j.pending(), []);
      assert.equal(logged.mock.callCount(), 1);
    } finally {
      j.close();
      f.cleanup();
    }
  });

// A reject frame that does not match the journal's own command is refused like
// a malformed ack: connectBridge fails, which closes the socket, and runBridge
// (src/cli.ts) reconnects. Nothing was dropped, so the next connection sends
// the same events again. r1's terminal observation is journaled, so a
// well-formed reject would refuse the run, release its folder (D-2') and print
// a notice; a malformed one does none of that.
// Mutation: drop the code check in src/journal.ts reject() and "a reject
// without its code" connects without error (Missing expected rejection).
type Receipt = Record<string, unknown>;
for (const [frame, malformed, error] of [
  ["an ack without its canonicalSeq", (receipt: Receipt) => ({ ack: receipt }), /acknowledgement identity mismatch/],
  ["a reject without its code", (receipt: Receipt) => ({ reject: receipt }), /rejection identity mismatch/],
  [
    "a reject whose sourceSeq is a string",
    (receipt: Receipt) => ({ reject: { ...receipt, sourceSeq: String(receipt.sourceSeq), code: "run_access_denied" } }),
    /rejection identity mismatch/,
  ],
  [
    "a reject without its sourceId",
    ({ sourceId: _, ...receipt }: Receipt) => ({ reject: { ...receipt, code: "run_access_denied" } }),
    /unknown rejection/,
  ],
] as const)
  test(`${frame} closes the socket, and the reconnect sends the events again`, async (t) => {
    const { connectBridge } = await import("../src/connection.ts");
    const logged = t.mock.method(console, "error", () => {});
    const f = fixture();
    const r1 = start(randomUUID());
    const j = new Journal(f.path, () => options);
    const stop = new AbortController();
    const sockets: { sent: string[]; closed: boolean }[] = [];
    let polls = 0;
    class Socket extends EventTarget {
      static OPEN = 1;
      readyState = 1;
      seen = { sent: [] as string[], closed: false };
      constructor() {
        super();
        sockets.push(this.seen);
        queueMicrotask(() => this.dispatchEvent(new Event("open")));
      }
      reply(value: unknown) {
        queueMicrotask(() =>
          this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) })),
        );
      }
      send(raw: string) {
        const value = JSON.parse(raw);
        if (value.poll) {
          // Ends a connection that wrongly survives the malformed frame.
          if ((polls += 1) === 5) return stop.abort();
          this.reply({ commands: [], lease: { deviceId: r1.deviceId, expiresAt: later(), runs: {} } });
          return;
        }
        this.seen.sent.push(`${value.runId}#${value.sourceSeq}`);
        this.reply(
          malformed({ runId: value.runId, sourceId: value.sourceId, sourceSeq: value.sourceSeq, generation: value.generation }),
        );
      }
      close() {
        if (this.readyState !== 3) {
          this.readyState = 3;
          this.seen.closed = true;
          // Asynchronous, as a real WebSocket: the bridge's own error surfaces.
          queueMicrotask(() => this.dispatchEvent(new Event("close")));
        }
      }
    }
    withSocket(t, Socket);
    try {
      await j.execute(r1, codex("r1")); // r1#1 observation:running
      j.recordNative(r1.commandId, { kind: "terminal", status: "completed", sessionId: "s-r1", turnId: "t-r1" }); // r1#2
      for (const attempt of ["first connection", "reconnect"])
        await assert.rejects(
          connectBridge({
            url: "wss://example.test/api/v1/harness/connect",
            deviceId: r1.deviceId,
            credentials: { accessToken: "jwt", grantToken: "grant" },
            journal: j,
            adapterFor: () => codex("r1"),
            signal: until(stop),
            timing: { pollMs: 10, livenessMs: 1000 },
          }),
          error,
          attempt,
        );
      const both = [`${r1.runId}#1`, `${r1.runId}#2`];
      assert.deepEqual(sockets, [
        { sent: both, closed: true },
        { sent: both, closed: true },
      ]);
      assert.deepEqual(queued(j), both);
      assert.equal(j.state(r1.commandId), "terminal_pending");
      assert.equal(j.workspaceLocked(r1.workspaceId), true);
      assert.equal(logged.mock.callCount(), 0);
    } finally {
      j.close();
      f.cleanup();
    }
  });
