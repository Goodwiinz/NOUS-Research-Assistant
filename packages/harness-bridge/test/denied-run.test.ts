import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Journal } from "../src/journal.ts";
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

/** Codex double: turns start, and history reports the turn as still running. */
function codex(name: string) {
  const value = {
    interrupts: 0,
    async startSession() {
      return { id: `s-${name}` };
    },
    async resumeSession(id: string) {
      return { id };
    },
    async startTurn() {
      return { id: `t-${name}` };
    },
    async interruptTurn() {
      value.interrupts += 1;
    },
    async respondToRequest() {},
    async inspectTurn(sessionId: string) {
      return { state: "running", sessionId, turnId: `t-${name}` };
    },
    async closeSession() {},
    async *events() {},
  };
  return value as unknown as HarnessAdapter & { interrupts: number };
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
