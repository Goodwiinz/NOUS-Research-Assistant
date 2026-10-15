import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { parseCommand } from "../src/connection.ts";
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
