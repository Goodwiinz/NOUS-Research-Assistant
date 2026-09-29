import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { nativeRequestBody } from "../src/approvals.ts";
import { Journal } from "../src/journal.ts";
import type { BridgeCommand } from "../src/connection.ts";
import type { AdapterEvent, HarnessAdapter } from "../src/contracts.ts";

const request = (
  overrides: Partial<Extract<AdapterEvent, { kind: "request" }>> = {},
): Extract<AdapterEvent, { kind: "request" }> => ({
  kind: "request",
  sessionId: "session-1",
  turnId: "turn-1",
  itemId: "item-1",
  requestId: 7,
  approvalId: "approve-1",
  method: "item/commandExecution/requestApproval",
  params: {
    threadId: "session-1",
    turnId: "turn-1",
    itemId: "item-1",
    approvalId: "approve-1",
    command: "pytest -q",
  },
  ...overrides,
});

test("retains native request ID type and complete request identity", () => {
  const value = nativeRequestBody(request({ requestId: "7" }));
  assert.deepEqual(value, {
    kind: "request",
    sessionId: "session-1",
    turnId: "turn-1",
    itemId: "item-1",
    requestId: "7",
    approvalId: "approve-1",
    method: "item/commandExecution/requestApproval",
    params: {
      threadId: "session-1",
      turnId: "turn-1",
      itemId: "item-1",
      approvalId: "approve-1",
      command: "pytest -q",
    },
  });
});

test("allows one-shot file approval and required input callbacks", () => {
  assert.equal(
    nativeRequestBody(
      request({
        method: "item/fileChange/requestApproval",
        params: {
          threadId: "session-1",
          turnId: "turn-1",
          itemId: "item-1",
        },
      }),
    ).kind,
    "request",
  );
  assert.equal(
    nativeRequestBody(
      request({
        method: "item/tool/requestUserInput",
        params: {
          threadId: "session-1",
          turnId: "turn-1",
          itemId: "item-1",
          isBlocking: true,
          questions: [{ id: "q1", header: "Version", question: "Which version?" }],
        },
      }),
    ).kind,
    "request",
  );
  for (const grantRoot of ["/outside-workspace", ""]) {
    assert.throws(
      () =>
        nativeRequestBody(
          request({
            method: "item/fileChange/requestApproval",
            params: {
              threadId: "session-1",
              turnId: "turn-1",
              itemId: "item-1",
              grantRoot,
            },
          }),
        ),
      /root changes are unsupported/,
    );
  }
});

test("rejects persistent permission changes and unknown request kinds", () => {
  assert.throws(() =>
    nativeRequestBody(
      request({
        params: {
          threadId: "session-1",
          turnId: "turn-1",
          itemId: "item-1",
          command: "pytest -q",
          proposedExecpolicyAmendment: [],
        },
      }),
    ),
  );
  assert.throws(() =>
    nativeRequestBody(request({ method: "item/permissions/requestApproval" })),
  );
});

test("rejects callback identity drift and duplicate question IDs", () => {
  assert.throws(() =>
    nativeRequestBody(request({ params: { threadId: "different" } })),
  );
  assert.throws(() =>
    nativeRequestBody(
      request({
        method: "item/tool/requestUserInput",
        params: {
          threadId: "session-1",
          turnId: "turn-1",
          itemId: "item-1",
          isBlocking: true,
          questions: [
            { id: "q1", header: "One", question: "One?" },
            { id: "q1", header: "Two", question: "Two?" },
          ],
        },
      }),
    ),
  );
});

test("response command redelivery reuses its durable local receipt", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-approval-journal-"));
  const journal = new Journal(join(dir, "journal.sqlite"), () => ({}) as never);
  const start: BridgeCommand = {
    deviceId: randomUUID(),
    runId: randomUUID(),
    commandId: randomUUID(),
    workspaceId: randomUUID(),
    generation: 1,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    body: { kind: "start", input: "hello" },
  };
  let responses = 0;
  const adapter = {
    async startSession() {
      return { id: "session-1" };
    },
    async startTurn() {
      return { id: "turn-1" };
    },
    async respondToRequest() {
      responses++;
    },
  } as unknown as HarnessAdapter;
  try {
    await journal.execute(start, adapter);
    journal.recordNative(start.commandId, {
      kind: "request",
      sessionId: "session-1",
      turnId: "turn-1",
      itemId: "item-1",
      requestId: 7,
      approvalId: "approve-1",
      method: "item/commandExecution/requestApproval",
      params: {
        threadId: "session-1",
        turnId: "turn-1",
        itemId: "item-1",
        approvalId: "approve-1",
        command: "pytest -q",
      },
    });
    const response: BridgeCommand = {
      ...start,
      commandId: randomUUID(),
      body: {
        kind: "respond",
        requestId: 7,
        response: { kind: "decision", allow: true },
        approvalRecordId: randomUUID(),
      },
    };
    await journal.execute(response, adapter);
    await journal.execute(response, adapter);
    assert.equal(responses, 1);
    const receipts = journal
      .pending()
      .filter((event) => event.body.kind === "command_ack");
    assert.equal(receipts.length, 1);
    assert.equal(receipts[0]?.commandId, response.commandId);
  } finally {
    journal.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
