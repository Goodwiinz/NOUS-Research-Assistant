import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { CodexAdapter } from "../src/adapters/codex.ts";
import { probeCodex } from "../src/cli.ts";

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
