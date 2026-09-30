import { randomUUID } from "node:crypto";
import { expect, test, type Response } from "@playwright/test";

const optIn = process.env.NOUS_HARNESS_E2E === "1";
const deviceId = process.env.NOUS_HARNESS_E2E_DEVICE_ID;
const required = {
  NOUS_HARNESS_E2E_WORKSPACE_ID: process.env.NOUS_HARNESS_E2E_WORKSPACE_ID,
  NOUS_HARNESS_E2E_THREAD_ID: process.env.NOUS_HARNESS_E2E_THREAD_ID,
  NOUS_HARNESS_E2E_EMAIL: process.env.NOUS_HARNESS_E2E_EMAIL,
  NOUS_HARNESS_E2E_PASSWORD: process.env.NOUS_HARNESS_E2E_PASSWORD,
};

function collectRunIds(response: Response, runIds: Set<string>): Promise<void> {
  if (
    !/\/agent\/stream(?:\/resume\/|$)/.test(new URL(response.url()).pathname)
  ) {
    return Promise.resolve();
  }
  return response
    .text()
    .then((body) => {
      for (const match of body.matchAll(/^data:\s*(\{.*\})\s*$/gm)) {
        try {
          const frame = JSON.parse(match[1]) as { run_id?: unknown };
          if (typeof frame.run_id === "string") runIds.add(frame.run_id);
        } catch {
          // Other SSE frames are not acceptance identities.
        }
      }
    })
    .catch(() => {
      // A response abandoned by the deliberate page reload has no full body.
    });
}

test("live Codex run survives chat reload and approval without duplicate transcript", async ({
  page,
}, testInfo) => {
  test.setTimeout(180_000);
  test.skip(
    !deviceId,
    "Live acceptance not run: no NOUS_HARNESS_E2E_DEVICE_ID is configured.",
  );
  test.skip(
    !optIn,
    "Live acceptance not opted in; set NOUS_HARNESS_E2E=1 only for the dedicated test device.",
  );
  const missing = Object.entries(required)
    .filter(([, value]) => !value)
    .map(([name]) => name);
  test.skip(
    missing.length > 0,
    `Live acceptance not run: missing dedicated prerequisites: ${missing.join(", ")}.`,
  );

  const workspaceId = required.NOUS_HARNESS_E2E_WORKSPACE_ID!;
  const threadId = required.NOUS_HARNESS_E2E_THREAD_ID!;
  const email = required.NOUS_HARNESS_E2E_EMAIL!;
  const password = required.NOUS_HARNESS_E2E_PASSWORD!;
  const marker = `NOUS_HARNESS_E2E_${randomUUID().replaceAll("-", "")}`;
  const fixtureFile = `.nous-harness-e2e-${marker.slice(-12)}.txt`;
  const command = `printf '%s' '${marker}' > '${fixtureFile}'`;
  const runIds = new Set<string>();
  const streamReads: Promise<void>[] = [];
  page.on("response", (response) => {
    streamReads.push(collectRunIds(response, runIds));
  });

  await page.goto("/login");
  await page.getByTestId("email-input").fill(email);
  await page.getByTestId("password-input").fill(password);
  await page.getByTestId("login-button").click();
  await page.waitForURL(/\/dashboard(?:\/|$)/, { timeout: 15_000 });
  await page.goto(`/chat?thread=${encodeURIComponent(threadId)}`);

  const provider = page.getByRole("combobox", { name: "Execution provider" });
  await expect(provider).toBeVisible();
  await provider.selectOption("codex");
  await page
    .getByRole("combobox", { name: "Paired computer" })
    .selectOption(deviceId!);
  await page
    .getByRole("combobox", { name: "Project workspace" })
    .selectOption(workspaceId);

  const composer = page.getByPlaceholder(
    "Ask anything, or paste a passage to discuss…",
  );
  await expect(composer).toBeEnabled();
  await composer.fill(
    `In the registered test workspace, execute this exact command: ${command}. If it succeeds, reply with this exact marker: ${marker}. Do not use another command.`,
  );
  await page.getByRole("button", { name: /^Send/ }).click();

  const permission = page.getByRole("alertdialog", {
    name: "Codex permission request",
  });
  await expect(permission).toBeVisible({ timeout: 120_000 });
  await expect(permission).toContainText(command);

  // A refresh drops only browser observation. The exact challenge must be
  // replayed from the durable run before the user approves it once.
  await page.reload();
  await expect(
    page.getByRole("alertdialog", { name: "Codex permission request" }),
  ).toBeVisible({ timeout: 60_000 });
  const replayedPermission = page.getByRole("alertdialog", {
    name: "Codex permission request",
  });
  await expect(replayedPermission).toContainText(command);

  const decisionResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/harness\/requests\/[^/]+\/decision$/.test(
        new URL(response.url()).pathname,
      ),
  );
  await replayedPermission.getByRole("button", { name: "Allow once" }).click();
  const decision = await decisionResponse;
  expect(decision.ok()).toBe(true);
  expect(decision.request().postDataJSON()).toMatchObject({
    kind: "decision",
    allow: true,
  });

  const assistantMarker = page
    .locator('[data-role="assistant"]')
    .filter({ hasText: marker });
  await expect(assistantMarker).toHaveCount(1, { timeout: 120_000 });
  await page.reload();
  await expect(
    page.locator('[data-role="assistant"]').filter({ hasText: marker }),
  ).toHaveCount(1, { timeout: 60_000 });
  await expect(
    page.locator('[data-role="user"]').filter({ hasText: marker }),
  ).toHaveCount(1);
  await Promise.allSettled(streamReads);

  const runId = [...runIds][0];
  expect(
    runId,
    "the accepted durable run_id must be present in persisted SSE",
  ).toBeTruthy();
  testInfo.annotations.push({
    type: "live-harness-acceptance",
    description: `outcome=completed thread_id=${threadId} run_id=${runId} device_id=${deviceId} workspace_id=${workspaceId}`,
  });
});
