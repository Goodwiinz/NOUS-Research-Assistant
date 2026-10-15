import assert from "node:assert/strict";
import { test } from "node:test";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { CodexAdapter } from "../src/adapters/codex.ts";
import { probeCodex } from "../src/cli.ts";
import { CredentialStore } from "../src/credentials.ts";

const packageDir = fileURLToPath(new URL("..", import.meta.url));

test("rc refuses without a terminal before touching Codex, the network or the store", () => {
  const root = mkdtempSync(join(tmpdir(), "nous-rc-main-root-"));
  const store = mkdtempSync(join(tmpdir(), "nous-rc-main-store-"));
  try {
    // Port 9 (discard) is never reached: the refusal comes first.
    const result = spawnSync(
      process.execPath,
      [
        "--import",
        "tsx",
        "src/cli.ts",
        "rc",
        "--api",
        "http://127.0.0.1:9/api/v1",
        "--root",
        root,
        "--store",
        store,
      ],
      {
        cwd: packageDir,
        stdio: ["ignore", "pipe", "pipe"],
        encoding: "utf8",
        timeout: 30_000,
      },
    );
    assert.match(result.stderr, /nous --rc needs an interactive terminal/);
    assert.equal(result.status, 1);
    assert.deepEqual(readdirSync(store), []);
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(store, { recursive: true, force: true });
  }
});

test("a Codex that cannot run at all gets an install hint", async () => {
  await assert.rejects(
    probeCodex(new CodexAdapter({ command: "/nonexistent/codex" })),
    (error: Error) => {
      assert.equal(
        error.message,
        "Could not run `codex --version`; check that Codex is installed, on your PATH and working",
      );
      assert.equal((error.cause as Error).message, "Codex version probe failed");
      return true;
    },
  );
});

test("nous-harness run stops on SIGHUP (terminal closed) and releases the lock", async () => {
  const store = mkdtempSync(join(tmpdir(), "nous-rc-main-run-"));
  try {
    // A paired device whose NOUS (port 9, discard) never answers: the bridge
    // sits in its reconnect loop until it is stopped.
    const credentials = new CredentialStore(store);
    const credentialHandle = await credentials.save({
      accessToken: "cli-secret",
      grantToken: "grant-secret",
      grantId: "33333333-3333-4333-8333-333333333333",
      renewedAt: Date.now(),
    });
    await credentials.writeLocal("connection", {
      apiUrl: "http://127.0.0.1:9/api/v1",
      deviceId: "22222222-2222-4222-8222-222222222222",
      projectId: "11111111-1111-4111-8111-111111111111",
      label: "Laptop",
      credentialHandle,
      scopes: ["harness:execute"],
      workspaces: [],
    });
    const child = spawn(process.execPath, ["--import", "tsx", "src/cli.ts", "run", "--store", store], {
      cwd: packageDir,
      stdio: ["ignore", "ignore", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => (stderr += String(chunk)));
    const exited = once(child, "exit");
    const lock = join(store, "bridge.lock");
    // The lock appears before main installs its signal handlers; this line
    // only once the bridge is in its reconnect loop, after them.
    const running = () => stderr.includes("Bridge disconnected");
    for (let waited = 0; !running() && waited < 20_000; waited += 50) await sleep(50);
    assert.ok(running(), `the bridge never started: ${stderr}`);
    assert.ok(existsSync(lock));
    child.kill("SIGHUP");
    const stuck = setTimeout(() => child.kill("SIGKILL"), 20_000);
    const [code, signal] = await exited;
    clearTimeout(stuck);
    assert.deepEqual([code, signal], [0, null], stderr);
    assert.equal(existsSync(lock), false);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});
