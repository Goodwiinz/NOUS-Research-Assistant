import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// The repo-root `nous` launcher: `nous --rc` must run this package's CLI on
// Node 24 without touching the caller's PATH, from any folder, through a
// symlink, and refuse a copy that is not inside a checkout.
const launcher = fileURLToPath(new URL("../../../nous", import.meta.url));
const packageDir = realpathSync(fileURLToPath(new URL("..", import.meta.url)));
const DEFAULT_API = "https://dev-api.goodwiinz.tech/api/v1";
const PROBES = "/opt/homebrew/opt/node@24/bin /usr/local/opt/node@24/bin";

/** A fake `node`: answers --version, otherwise prints itself, PATH, cwd and argv one per line. */
function stubNode(dir: string, version: string): string {
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "node");
  writeFileSync(
    path,
    [
      "#!/bin/sh",
      `if [ "$1" = "--version" ]; then echo "${version}"; exit 0; fi`,
      'echo "$0"',
      'echo "$PATH"',
      "pwd -P",
      'for arg in "$@"; do echo "$arg"; done',
      "",
    ].join("\n"),
    { mode: 0o755 },
  );
  return path;
}

function sandbox(t: { after: (fn: () => void) => void }) {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "nous-launcher-")));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const stubs = join(dir, "stubs");
  const node = stubNode(stubs, "v24.0.0");
  const cwd = join(dir, "my project");
  mkdirSync(cwd);
  return { dir, stubs, node, cwd, path: `${stubs}:/usr/bin:/bin` };
}

function run(script: string, args: string[], cwd: string, path: string, env: Record<string, string> = {}) {
  const result = spawnSync("/bin/bash", [script, ...args], {
    cwd,
    env: { PATH: path, ...env },
    encoding: "utf8",
  });
  return { status: result.status, stdout: result.stdout, stderr: result.stderr };
}

/** The stub's report: which node ran, with what PATH, from where, and its argv. */
function report(stdout: string) {
  const [node, path, cwd, ...argv] = stdout.replace(/\n$/, "").split("\n");
  return { node, path, cwd, argv };
}

/** The value after the last occurrence of `flag`, which is what the CLI's parseArgs keeps. */
function last(argv: string[], flag: string): string | undefined {
  const at = argv.lastIndexOf(flag);
  return at === -1 ? undefined : argv[at + 1];
}

test("nous --rc runs the bridge CLI on Node 24 with the caller's folder, through a symlink", (t) => {
  const { dir, node, cwd, path } = sandbox(t);
  const link = join(dir, "bin", "nous");
  mkdirSync(join(dir, "bin"));
  symlinkSync(launcher, link);

  const result = run(link, ["--rc", "--label", "a b", "--root", "/elsewhere", "--api", "https://elsewhere.test/api/v1"], cwd, path);

  assert.equal(result.stderr, "");
  assert.equal(result.status, 0);
  const ran = report(result.stdout);
  assert.equal(ran.node, node);
  assert.equal(ran.cwd, packageDir, "runs from the package so src/cli.ts and tsx resolve");
  assert.deepEqual(ran.argv.slice(0, 4), ["--import", "tsx", "src/cli.ts", "rc"]);
  const label = ran.argv.indexOf("--label");
  assert.deepEqual(ran.argv.slice(label, label + 2), ["--label", "a b"]);
  assert.equal(last(ran.argv, "--root"), cwd, "the launcher's --root wins over the user's");
  assert.equal(last(ran.argv, "--api"), DEFAULT_API, "the launcher's --api wins over the user's");
  assert.equal(ran.path, path, "PATH reaches the bridge (and so Codex) unchanged");
});

test("nous --rc follows NOUS_API_URL through a relative symlink chain", (t) => {
  const { dir, cwd, path } = sandbox(t);
  mkdirSync(join(dir, "links"));
  mkdirSync(join(dir, "bin"));
  symlinkSync(launcher, join(dir, "links", "nous1"));
  symlinkSync("nous1", join(dir, "links", "nous2"));
  symlinkSync("../links/nous2", join(dir, "bin", "nous"));

  const result = run(join(dir, "bin", "nous"), ["--rc", "--api", "https://elsewhere.test/api/v1"], cwd, path, {
    NOUS_API_URL: "https://nous.test/api/v1",
  });

  assert.equal(result.stderr, "");
  assert.equal(result.status, 0);
  const ran = report(result.stdout);
  assert.equal(ran.cwd, packageDir);
  assert.equal(last(ran.argv, "--api"), "https://nous.test/api/v1");
  assert.equal(last(ran.argv, "--root"), cwd);
  assert.equal(ran.path, path);
});

test("a copied launcher refuses before running anything", (t) => {
  const { dir, cwd, path } = sandbox(t);
  mkdirSync(join(dir, "copy"));
  const copy = join(dir, "copy", "nous");
  writeFileSync(copy, readFileSync(launcher), { mode: 0o755 });

  const result = run(copy, ["--rc"], cwd, path);

  assert.equal(result.status, 1);
  assert.equal(result.stdout, "");
  assert.equal(
    result.stderr,
    `nous: ${join(dir, "copy")} is not a NOUS checkout; link this launcher with ln -s instead of copying it\n`,
  );
});

/**
 * A minimal fake checkout whose launcher probes `brew` folders under the
 * sandbox instead of Homebrew's, so these paths behave the same on any machine.
 */
function fakeCheckout(dir: string, { installed }: { installed: boolean }) {
  const repo = join(dir, "repo");
  mkdirSync(join(repo, "packages", "harness-bridge"), { recursive: true });
  writeFileSync(join(repo, "packages", "harness-bridge", "package.json"), "{}\n");
  if (installed) {
    mkdirSync(join(repo, "node_modules", "tsx"), { recursive: true });
    writeFileSync(join(repo, "node_modules", "tsx", "package.json"), "{}\n");
  }
  const source = readFileSync(launcher, "utf8");
  assert.ok(source.includes(PROBES), "the launcher's Homebrew probe folders moved; update this test");
  const brewA = join(dir, "brew-a", "bin");
  const brewB = join(dir, "brew-b", "bin");
  writeFileSync(join(repo, "nous"), source.replace(PROBES, `${brewA} ${brewB}`), { mode: 0o755 });
  return { repo, script: join(repo, "nous"), brewA, brewB };
}

test("an older default node falls back to a Node 24 keg without changing PATH", (t) => {
  const { dir, cwd } = sandbox(t);
  const old = join(dir, "old");
  stubNode(old, "v22.0.0");
  const { repo, script, brewA, brewB } = fakeCheckout(dir, { installed: true });
  stubNode(brewA, "v23.0.0");
  const keg = stubNode(brewB, "v24.1.0");
  const path = `${old}:/usr/bin:/bin`;

  const result = run(script, ["--rc"], cwd, path);

  assert.equal(result.stderr, "");
  assert.equal(result.status, 0);
  const ran = report(result.stdout);
  assert.equal(ran.node, keg);
  assert.equal(ran.path, path, "no keg folder prepended to PATH");
  assert.equal(ran.cwd, join(repo, "packages", "harness-bridge"));
  assert.equal(last(ran.argv, "--root"), cwd);
});

test("no Node 24 anywhere refuses with the install hint", (t) => {
  const { dir, cwd } = sandbox(t);
  const old = join(dir, "old");
  stubNode(old, "v22.0.0");
  const { script } = fakeCheckout(dir, { installed: true });

  const result = run(script, ["--rc"], cwd, `${old}:/usr/bin:/bin`);

  assert.equal(result.status, 1);
  assert.equal(result.stdout, "");
  assert.equal(
    result.stderr,
    "nous --rc needs Node 24 (found 22.0.0); switch your default node to 24 (for example nvm use 24) or install it with: brew install node@24\n",
  );
});

test("a checkout without dependencies refuses with the install command", (t) => {
  const { dir, cwd, path } = sandbox(t);
  const { repo, script } = fakeCheckout(dir, { installed: false });

  const result = run(script, ["--rc"], cwd, path);

  assert.equal(result.status, 1);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, `nous --rc: dependencies are missing; run: pnpm --dir "${repo}" install\n`);
});
