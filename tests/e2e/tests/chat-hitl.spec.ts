import { test, expect, type Page, type Route } from "@playwright/test";
import { createTestHelpers, TEST_DATA } from "../utils/test-helpers";

/**
 * Critical journey: the human-in-the-loop approval gate. A destructive tool
 * call (e.g. arXiv ingest) pauses the turn and shows an in-band approval
 * banner (HitlApprovalToolUI); the user's Approve/Deny decision must resume
 * the turn via `/agent/stream/confirm` and produce the corresponding
 * outcome.
 *
 * The agent SSE endpoints are intercepted so both branches (approve and
 * reject) are deterministic — no dependency on a live model producing a
 * real tool call. Timing is driven by real network responses and web-first
 * assertions, no fixed sleeps.
 *
 * @regression
 */

const STREAM_URL = "**/api/v1/agent/stream";
const CONFIRM_URL = "**/api/v1/agent/stream/confirm";
const RESUME_URL = "**/agent/stream/resume/**";
const TOOL_NAME = "ingest_arxiv_papers";
// The banner renders the humanized vocabulary label from toolLabels.ts, not
// the raw tool name (HitlApprovalToolUI passes tool.name through toolLabel).
const TOOL_LABEL = "Ingest arXiv papers";
const APPROVED_MARKER = "hitl-approved";
const DENIED_MARKER = "hitl-denied";

function sseFrame(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Pauses the first turn on a HITL confirmation gate, then resolves the
 * resumed turn according to whatever `confirmed` the client actually sent —
 * so the same mock serves both the approve and the reject journey. */
async function installHitlMocks(page: Page): Promise<void> {
  await page.route(RESUME_URL, (route: Route) =>
    route.fulfill({ status: 204, body: "" }),
  );

  await page.route(STREAM_URL, async (route: Route) => {
    const threadId =
      (route.request().postDataJSON()?.thread_id as string | undefined) ??
      null;
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sseFrame("confirmation", {
        thread_id: threadId,
        confirmation: {
          tool_name: TOOL_NAME,
          tool_args: { paper_id: "2501.00001" },
        },
      }),
    });
  });

  await page.route(CONFIRM_URL, async (route: Route) => {
    const confirmed = Boolean(route.request().postDataJSON()?.confirmed);
    const marker = confirmed ? APPROVED_MARKER : DENIED_MARKER;
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body:
        sseFrame("token", { content: `Reply: ${marker}` }) +
        sseFrame("done", { thread_id: null }),
    });
  });
}

test.describe("Chat HITL approval gate @regression", () => {
  test.beforeEach(async ({ page }) => {
    await installHitlMocks(page);
  });

  async function startGatedTurn(page: Page): Promise<void> {
    const composer = page.getByPlaceholder(
      "Ask anything, or paste a passage to discuss…",
    );
    await expect(composer).toBeVisible({ timeout: 15000 });
    await composer.fill(`Ingest an arXiv paper for me: ${TOOL_NAME}`);
    await page.getByRole("button", { name: /^Send/ }).click();

    const banner = page.getByRole("alertdialog", { name: "Approval needed" });
    await expect(banner).toBeVisible({ timeout: 15000 });
    // Scope the label assertion to the banner: the transcript may echo
    // similar copy, so an unscoped getByText risks a strict-mode violation.
    await expect(banner.getByText(TOOL_LABEL)).toBeVisible();
  }

  test("approving the gate resumes the turn and clears the banner", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/chat?new=1");

    await startGatedTurn(page);

    await page.getByRole("button", { name: "Approve" }).click();

    await expect(
      page.getByRole("alertdialog", { name: "Approval needed" }),
    ).toHaveCount(0);
    await expect(
      page
        .locator('[data-role="assistant"]')
        .filter({ hasText: APPROVED_MARKER }),
    ).toBeVisible({ timeout: 15000 });
  });

  test("denying the gate resumes the turn without running the tool", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/chat?new=1");

    await startGatedTurn(page);

    await page.getByRole("button", { name: "Deny" }).click();

    await expect(
      page.getByRole("alertdialog", { name: "Approval needed" }),
    ).toHaveCount(0);
    await expect(
      page
        .locator('[data-role="assistant"]')
        .filter({ hasText: DENIED_MARKER }),
    ).toBeVisible({ timeout: 15000 });
    await expect(
      page
        .locator('[data-role="assistant"]')
        .filter({ hasText: APPROVED_MARKER }),
    ).toHaveCount(0);
  });
});
