import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { acquireStateLock } from "../src/cli.ts";

const packageDir = fileURLToPath(new URL("..", import.meta.url));

function setup() {
  const dir = mkdtempSync(join(tmpdir(), "nous-lock-"));
  const lock = join(dir, "bridge.lock");
  return {
    dir,
    lock,
    holder: () => JSON.parse(readFileSync(lock, "utf8")).pid as number,
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

/** A pid that has already exited (and been reaped). */
function deadPid(): number {
  const child = spawnSync(process.execPath, ["-e", ""]);
  assert.equal(child.status, 0);
  return child.pid!;
}

test("the lock is owner-only and names this process", async () => {
  const s = setup();
  try {
    const release = await acquireStateLock(s.dir);
    assert.equal(s.holder(), process.pid);
    assert.equal(statSync(s.lock).mode & 0o777, 0o600);
    assert.equal(typeof JSON.parse(readFileSync(s.lock, "utf8")).startedAt, "string");
    await release();
  } finally {
    s.cleanup();
  }
});

test("a lock held by a live process refuses with its pid", async () => {
  const s = setup();
  try {
    // The test runner that spawned this file is alive for its whole run.
    writeFileSync(s.lock, JSON.stringify({ pid: process.ppid, startedAt: "x" }), { mode: 0o600 });
    await assert.rejects(acquireStateLock(s.dir), {
      message: `Another nous --rc or nous-harness run (pid ${process.ppid}) is already using ${s.dir}; stop it first`,
    });
    assert.equal(s.holder(), process.ppid);
    // A second acquire in the holding process refuses the same way.
    rmSync(s.lock);
    const release = await acquireStateLock(s.dir);
    await assert.rejects(acquireStateLock(s.dir), { message: new RegExp(`\\(pid ${process.pid}\\)`) });
    await release();
  } finally {
    s.cleanup();
  }
});

test("a lock whose process is gone, or that cannot be read, is replaced", async () => {
  const s = setup();
  try {
    writeFileSync(s.lock, JSON.stringify({ pid: deadPid(), startedAt: "x" }), { mode: 0o600 });
    const release = await acquireStateLock(s.dir);
    assert.equal(s.holder(), process.pid);
    await release();
    writeFileSync(s.lock, "not json", { mode: 0o600 });
    const again = await acquireStateLock(s.dir);
    assert.equal(s.holder(), process.pid);
    await again();
  } finally {
    s.cleanup();
  }
});

test("release removes only this process's lock, and the lock can be taken again", async () => {
  const s = setup();
  try {
    const release = await acquireStateLock(s.dir);
    await release();
    assert.equal(existsSync(s.lock), false);
    const second = await acquireStateLock(s.dir);
    assert.equal(s.holder(), process.pid);
    // Another process took the folder over: our release must leave its lock.
    writeFileSync(s.lock, JSON.stringify({ pid: process.ppid, startedAt: "x" }), { mode: 0o600 });
    await second();
    assert.equal(s.holder(), process.ppid);
  } finally {
    s.cleanup();
  }
});

test("process.exit releases the lock", () => {
  const s = setup();
  try {
    const script = `const { acquireStateLock } = await import(${JSON.stringify(join(packageDir, "src/cli.ts"))}); await acquireStateLock(${JSON.stringify(s.dir)}); process.exit(130);`;
    const child = spawnSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", script], {
      cwd: packageDir,
      encoding: "utf8",
      timeout: 30_000,
    });
    assert.equal(child.status, 130, child.stderr);
    assert.equal(existsSync(s.lock), false);
  } finally {
    s.cleanup();
  }
});
