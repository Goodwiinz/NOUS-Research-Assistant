import { test, expect, type Route } from "@playwright/test";
import { createTestHelpers, TEST_DATA } from "./utils/test-helpers";

/**
 * GOO-354: shared-browser account switching must not show the previous
 * account's chat. Matrix: docs/engineering/data-isolation-matrix.md.
 *
 * Account A puts a canary into the chat transcript (agent stream
 * intercepted, so nothing depends on a model), signs out, and account B signs
 * in on the same page. B's view must never render the canary, and the
 * persisted chat selection must not still point at A's thread.
 *
 * Opt-in (E2E_ACCOUNT_SWITCH=1) until it has run once against the CI stack;
 * the in-memory store clearing it checks is GOO-350 (PR #1776).
 */

const STREAM_URL = "**/api/v1/agent/stream";
const RESUME_URL = "**/agent/stream/resume/**";

test.describe("Account switch isolation @regression", () => {
  test.skip(
    !process.env.E2E_ACCOUNT_SWITCH,
    "opt-in until verified on the CI stack (GOO-354)",
  );

  test("account B never sees account A's chat after a same-tab switch", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    const canary = `iso-canary-${Date.now()}`;

    await page.route(RESUME_URL, (route: Route) =>
      route.fulfill({ status: 204, body: "" }),
    );
    await page.route(STREAM_URL, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body:
          `event: token\ndata: ${JSON.stringify({ content: `Reply ${canary}` })}\n\n` +
          `event: done\ndata: ${JSON.stringify({ thread_id: null })}\n\n`,
      }),
    );

    // --- Account A ---
    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/chat?new=1");
    const composer = page.getByPlaceholder(
      "Ask anything, or paste a passage to discuss…",
    );
    await expect(composer).toBeVisible({ timeout: 15000 });
    await composer.fill(`Private ${canary}`);
    await page.getByRole("button", { name: /^Send/ }).click();
    await expect(
      page.locator('[data-role="assistant"]').filter({ hasText: canary }),
    ).toBeVisible({ timeout: 15000 });
    await page.waitForURL(/\/chat\?thread=/, { timeout: 15000 });
    const threadA = new URL(page.url()).searchParams.get("thread");

    // --- Same tab: sign out, sign in as B ---
    await helpers.logout();
    await helpers.login(TEST_DATA.USERS.REGULAR);
    await helpers.navigateTo("/chat");
    await expect(composer).toBeVisible({ timeout: 15000 });

    await expect(page.getByText(canary)).toHaveCount(0);
    const persisted = await page.evaluate(
      () => window.localStorage.getItem("chat-storage") ?? "",
    );
    expect(threadA).toBeTruthy();
    expect(persisted).not.toContain(threadA as string);
    expect(persisted).not.toContain(canary);
  });
});
