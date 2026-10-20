import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { parseCommand } from "../src/connection.ts";
import { sessionOptionsFor, type LocalState } from "../src/cli.ts";
import { Journal } from "../src/journal.ts";
import type { HarnessAdapter, SessionOptions } from "../src/contracts.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";

function start(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    deviceId: randomUUID(),
    runId: randomUUID(),
    commandId: randomUUID(),
    workspaceId: randomUUID(),
    generation: 1,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    body: { kind: "start", input: "hello" },
    ...extra,
  };
}

test("a start command may carry its run's projectId; other commands and malformed ids are refused", () => {
  assert.equal(parseCommand(start({ projectId: PROJECT })).projectId, PROJECT);
  assert.equal("projectId" in parseCommand(start()), false);
  for (const projectId of ["not-a-uuid", null, 7])
    assert.throws(() => parseCommand(start({ projectId })), /invalid bridge command/, String(projectId));
  assert.throws(
    () => parseCommand(start({ projectId: PROJECT, body: { kind: "interrupt", sessionId: "s", turnId: "t" } })),
    /invalid bridge command/,
  );
});

test("the journal gives the start's projectId to the session options and never uploads it", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-run-project-"));
  const seen: (string | undefined)[] = [];
  const options: SessionOptions = {
    cwd: dir,
    workspaceId: "local",
    policy: {
      sandbox: "workspace-write",
      approvalPolicy: "on-request",
      reviewer: "user",
      networkAccess: false,
      writableRoots: [dir],
    },
  };
  const adapter = {
    async startSession() {
      return { id: "session" };
    },
    async startTurn() {
      return { id: "turn" };
    },
  } as unknown as HarnessAdapter;
  const journal = new Journal(join(dir, "journal.sqlite"), (_workspaceId, projectId) => {
    seen.push(projectId);
    return options;
  });
  try {
    await journal.execute(parseCommand(start({ projectId: PROJECT })), adapter);
    assert.deepEqual(seen, [PROJECT]);
    const events = journal.pending(64);
    assert.ok(events.length > 0, "the running observation is journaled");
    for (const event of events) assert.equal("projectId" in event, false);
  } finally {
    journal.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("an account connection pins each run's MCP child to the start's project; a project connection never gets --project", () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-run-project-state-"));
  const folder = { id: randomUUID(), root: dir, label: "w" };
  const account: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    account: true,
    credentialHandle: randomUUID(),
    scopes: ["harness:execute", "tools:read"],
    workspaces: [folder],
  };
  try {
    const args = sessionOptionsFor(dir, account, folder.id, PROJECT).mcpConfig?.nous?.args ?? [];
    assert.equal(args[args.indexOf("--project") + 1], PROJECT);
    // Without the run's project an account run gets no NOUS tools rather than unpinned ones.
    assert.equal("mcpConfig" in sessionOptionsFor(dir, account, folder.id), false);
    const { account: _account, ...rest } = account;
    const project: LocalState = { ...rest, projectId: PROJECT, workspaces: [{ ...folder, projectId: PROJECT }] };
    const plain = sessionOptionsFor(dir, project, folder.id, randomUUID()).mcpConfig?.nous?.args ?? [];
    assert.equal(plain.includes("--project"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
