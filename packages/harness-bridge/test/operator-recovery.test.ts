import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readBootIdentity, type BootIdentity } from "../src/recovery.ts";
import { Journal } from "../src/journal.ts";
import { NativeExitUnconfirmed } from "../src/rpc.ts";
import type { BridgeCommand } from "../src/connection.ts";
import type { HarnessAdapter } from "../src/contracts.ts";

const initialBoot = (): BootIdentity => ({
  machineId: "a".repeat(64),
  bootId: randomUUID(),
  bootStartedAt: 1000,
  observedAt: 100_000,
});
function fixture(readBootIdentity: () => ReturnType<typeof initialBoot>) {
  const dir = mkdtempSync(join(tmpdir(), "nous-recovery-"));
  const j = new Journal(join(dir, "journal.sqlite"), () => ({}) as never, {
    readBootIdentity,
  });
  const start: BridgeCommand = {
    commandId: randomUUID(),
    deviceId: randomUUID(),
    runId: randomUUID(),
    workspaceId: randomUUID(),
    generation: 1,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    body: { kind: "start", input: "hello" },
  };
  const stop: BridgeCommand = {
    ...start,
    commandId: randomUUID(),
    body: { kind: "interrupt", sessionId: "s", turnId: "t" },
  };
  const a = {
    startSession: async () => ({ id: "s" }),
    startTurn: async () => ({ id: "t" }),
    interruptTurn: async () => {
      throw new NativeExitUnconfirmed();
    },
  } as unknown as HarnessAdapter;
  return {
    dir,
    j,
    start,
    stop,
    a,
    close() {
      j.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}
test("operator recovery requires a later boot on the same machine and clears only its interrupt", async () => {
  let boot = initialBoot();
  const f = fixture(() => boot),
    { j, start, stop, a } = f;
  const other = { ...stop, commandId: randomUUID() };
  try {
    await j.execute(start, a);
    await j.execute(stop, a);
    await j.execute(other, a);
    assert.throws(() => j.recoverInterrupt(stop.commandId), /same boot/);
    boot = {
      ...boot,
      machineId: "b".repeat(64),
      bootId: randomUUID(),
      bootStartedAt: 200_000,
      observedAt: 300_000,
    };
    assert.throws(
      () => j.recoverInterrupt(stop.commandId),
      /different machine/,
    );
    boot = { ...boot, machineId: "a".repeat(64), bootStartedAt: 102_000 };
    assert.throws(() => j.recoverInterrupt(stop.commandId), /predate.*boot/);
    boot = { ...boot, bootStartedAt: 200_000 };
    const states = [j.state(start.commandId), j.state(stop.commandId)];
    j.recoverInterrupt(stop.commandId);
    assert.deepEqual(j.uncertainInterrupts(), [other.commandId]);
    assert.deepEqual(
      [j.state(start.commandId), j.state(stop.commandId)],
      states,
    );
    assert.equal(j.workspaceLocked(start.workspaceId), true);
    let calls = 0;
    a.interruptTurn = async () => {
      calls++;
    };
    await j.execute(other, a);
    assert.equal(calls, 0);
    await j.execute(stop, a);
    assert.equal(calls, 1);
    assert.equal(j.state(stop.commandId), "delivered");
    assert.equal(j.workspaceLocked(start.workspaceId), true);
  } finally {
    f.close();
  }
});
test("missing boot evidence records a baseline but refuses recovery until a later verified reboot", async () => {
  let unavailable = true,
    boot = initialBoot();
  const f = fixture(() => {
    if (unavailable) throw new Error("OS evidence unavailable");
    return boot;
  });
  const { j, start, stop, a } = f;
  try {
    await j.execute(start, a);
    await j.execute(stop, a);
    assert.throws(
      () => j.recoverInterrupt(stop.commandId),
      /OS evidence unavailable/,
    );
    unavailable = false;
    assert.throws(
      () => j.recoverInterrupt(stop.commandId),
      /baseline recorded.*reboot/,
    );
    assert.throws(() => j.recoverInterrupt(stop.commandId), /same boot/);
    boot = {
      ...boot,
      bootId: randomUUID(),
      bootStartedAt: 200_000,
      observedAt: 300_000,
    };
    const reopened = new Journal(
      join(f.dir, "journal.sqlite"),
      () => ({}) as never,
      {
        recoveryOnly: true,
        readBootIdentity: () => boot,
      },
    );
    try {
      reopened.recoverInterrupt(stop.commandId);
    } finally {
      reopened.close();
    }
    assert.deepEqual(j.uncertainInterrupts(), []);
    assert.equal(j.workspaceLocked(start.workspaceId), true);
  } finally {
    f.close();
  }
});

function cli(...args: string[]) {
  return spawnSync(
    process.execPath,
    [
      "--experimental-sqlite",
      "--import",
      "tsx",
      fileURLToPath(new URL("../src/cli.ts", import.meta.url)),
      ...args,
    ],
    { encoding: "utf8", timeout: 10_000 },
  );
}
test("local CLI lists markers and refuses same-boot recovery without changing states", async () => {
  const identity = readBootIdentity();
  assert.match(identity.machineId, /^[a-f0-9]{64}$/);
  const f = fixture(() => identity),
    { j, start, stop, a } = f;
  try {
    await j.execute(start, a);
    await j.execute(stop, a);
    const before = [j.state(start.commandId), j.state(stop.commandId)];
    const listed = cli("recover-interrupt", "--store", f.dir);
    assert.equal(listed.status, 0, listed.stderr);
    assert.match(listed.stdout, new RegExp(stop.commandId));
    const refused = cli(
      "recover-interrupt",
      "--store",
      f.dir,
      "--command",
      stop.commandId,
    );
    assert.equal(refused.status, 1);
    assert.match(refused.stderr, /same boot/);
    assert.deepEqual(
      [j.state(start.commandId), j.state(stop.commandId)],
      before,
    );
    assert.equal(j.workspaceLocked(start.workspaceId), true);
    const help = cli("recover-interrupt", "--help");
    assert.equal(help.status, 0, help.stderr);
    assert.match(help.stdout, /baseline.*reboot/s);
    assert.match(help.stdout, /run state unchanged/);
  } finally {
    f.close();
  }
});
test("local CLI verifies OS identity against persisted prior-boot evidence and retains ownership", async () => {
  const current = readBootIdentity();
  const prior = {
    ...current,
    bootId: randomUUID(),
    bootStartedAt: current.bootStartedAt - 120_000,
    observedAt: current.bootStartedAt - 60_000,
  };
  const f = fixture(() => prior),
    { j, start, stop, a } = f;
  try {
    await j.execute(start, a);
    await j.execute(stop, a);
    const before = [j.state(start.commandId), j.state(stop.commandId)];
    const recovered = cli(
      "recover-interrupt",
      "--store",
      f.dir,
      "--command",
      stop.commandId,
    );
    assert.equal(recovered.status, 0, recovered.stderr);
    assert.match(recovered.stdout, /blocker cleared/);
    assert.deepEqual(j.uncertainInterrupts(), []);
    assert.deepEqual(
      [j.state(start.commandId), j.state(stop.commandId)],
      before,
    );
    assert.equal(j.workspaceLocked(start.workspaceId), true);
  } finally {
    f.close();
  }
});
