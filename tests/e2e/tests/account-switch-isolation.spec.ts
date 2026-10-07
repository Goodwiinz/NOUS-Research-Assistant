import { test, expect, type Page, type Route } from "@playwright/test";
import { createTestHelpers, TEST_DATA } from "./utils/test-helpers";

/**
 * GOO-354: shared-browser account switching must not show the previous
 * account's chat, agent panel, project, or search data. Matrix:
 * docs/engineering/data-isolation-matrix.md.
 *
 * Account A puts a canary into the chat transcript (agent stream
 * intercepted, so nothing depends on a model), signs out, and account B signs
 * in on the same page. B's view must never render the canary, and the
 * persisted chat selection must not still point at A's thread.
 *
 * This is part of the PR smoke lane. The in-memory store clearing it checks
 * is GOO-350 (PR #1854); the chat request lifetime is also guarded by #1776.
 */

const STREAM_URL = "**/api/v1/agent/stream";
const RESUME_URL = "**/agent/stream/resume/**";
const PROJECTS_URL = "**/api/v1/projects*";
const SEARCH_URL = "**/api/v1/search/hybrid";

async function signInOnCurrentPage(
  page: Page,
  credentials: { email: string; password: string },
): Promise<void> {
  await expect(page.getByTestId("email-input")).toBeVisible();
  await page.getByTestId("email-input").fill(credentials.email);
  await page.getByTestId("password-input").fill(credentials.password);
  await page.getByTestId("login-button").click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible({
    timeout: 20000,
  });
}

async function signOutOnCurrentPage(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL((url) => url.pathname === "/login");
}

async function followAppLink(page: Page, path: string): Promise<void> {
  await page.locator(`a[href="${path}"]`).first().click();
  await page.waitForURL((url) => url.pathname === path);
}

test.describe("Account switch isolation @smoke @regression", () => {
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
    await page.evaluate((value) => {
      (window as Window & { __isolationRuntime?: string }).__isolationRuntime =
        value;
    }, canary);

    // --- Same tab: sign out, sign in as B ---
    await signOutOnCurrentPage(page);
    await signInOnCurrentPage(page, TEST_DATA.USERS.REGULAR);
    if (new URL(page.url()).pathname !== "/chat") {
      await followAppLink(page, "/chat");
    }
    await expect(composer).toBeVisible({ timeout: 15000 });

    expect(
      await page.evaluate(
        () =>
          (window as Window & { __isolationRuntime?: string })
            .__isolationRuntime,
      ),
    ).toBe(canary);
    await expect(page.getByText(canary)).toHaveCount(0);
    const persisted = await page.evaluate(
      () => window.localStorage.getItem("chat-storage") ?? "",
    );
    expect(threadA).toBeTruthy();
    expect(persisted).not.toContain(threadA as string);
    expect(persisted).not.toContain(canary);
  });

  test("agent panel clears A's canary before B opens it", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    const marker = `iso-agent-${Date.now()}`;
    let account: "A" | "B" = "A";
    const answer = (owner: "A" | "B") => `${owner}-agent-${marker}`;
    await page.route(STREAM_URL, (route: Route) => {
      const owner = account;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body:
          `event: token\ndata: ${JSON.stringify({ content: answer(owner) })}\n\n` +
          `event: done\ndata: ${JSON.stringify({ thread_id: null })}\n\n`,
      });
    });

    await helpers.login(TEST_DATA.USERS.ADMIN);
    await page.getByRole("button", { name: "Open agent chat" }).click();
    let panel = page.locator('[role="dialog"][aria-label="Agent chat panel"]');
    await panel.getByPlaceholder("Ask the agent...").fill(marker);
    await panel.getByRole("button", { name: "Send message" }).click();
    await expect(panel.getByText(answer("A"))).toBeVisible();
    await page.evaluate((value) => {
      (window as Window & { __isolationRuntime?: string }).__isolationRuntime =
        value;
    }, marker);

    await signOutOnCurrentPage(page);
    account = "B";
    await signInOnCurrentPage(page, TEST_DATA.USERS.REGULAR);
    expect(
      await page.evaluate(
        () =>
          (window as Window & { __isolationRuntime?: string })
            .__isolationRuntime,
      ),
    ).toBe(marker);
    await page.getByRole("button", { name: "Open agent chat" }).click();
    panel = page.locator('[role="dialog"][aria-label="Agent chat panel"]');
    await expect(panel.getByText(answer("A"))).toHaveCount(0);
    await panel.getByPlaceholder("Ask the agent...").fill(`B-${marker}`);
    await panel.getByRole("button", { name: "Send message" }).click();
    await expect(panel.getByText(answer("B"))).toBeVisible();
    await expect(panel.getByText(answer("A"))).toHaveCount(0);
  });

  test("project and search canaries follow the active account", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    const marker = `iso-ui-${Date.now()}`;
    const lateQuery = `late-${marker}`;
    const lateAnswer = `A-late-search-${marker}`;
    let account: "A" | "B" = "A";
    const projectName = (owner: "A" | "B") => `${owner}-project-${marker}`;
    const searchAnswer = (owner: "A" | "B") => `${owner}-search-${marker}`;
    let notifyLateStarted!: () => void;
    let releaseLate!: () => void;
    let notifyLateSettled!: () => void;
    const lateStarted = new Promise<void>((resolve) => {
      notifyLateStarted = resolve;
    });
    const lateGate = new Promise<void>((resolve) => {
      releaseLate = resolve;
    });
    const lateSettled = new Promise<void>((resolve) => {
      notifyLateSettled = resolve;
    });

    await page.route(PROJECTS_URL, (route: Route) => {
      if (
        route.request().method() !== "GET" ||
        new URL(route.request().url()).pathname !== "/api/v1/projects"
      ) {
        return route.continue();
      }
      return route.fulfill({
        json: {
          projects: [
            {
              id:
                account === "A"
                  ? "11111111-1111-4111-8111-111111111111"
                  : "22222222-2222-4222-8222-222222222222",
              workspace_id: "33333333-3333-4333-8333-333333333333",
              name: projectName(account),
              description: `Synthetic ${account} project`,
              research_status: "active",
              tags: [],
              created_at: "2026-10-01T00:00:00Z",
              updated_at: "2026-10-01T00:00:00Z",
            },
          ],
          total: 1,
        },
      });
    });
    await page.route(SEARCH_URL, async (route: Route) => {
      // Capture request ownership before waiting: account changes to B while
      // the second A search remains in flight.
      const owner = account;
      const query = (route.request().postDataJSON() as { query: string }).query;
      const heldAResponse = owner === "A" && query === lateQuery;
      const response = {
        json: {
          search_id: `synthetic-${owner}`,
          query,
          synthesized_answer: heldAResponse ? lateAnswer : searchAnswer(owner),
          results: [],
          total_results: 0,
        },
      };
      if (!heldAResponse) return route.fulfill(response);
      notifyLateStarted();
      await lateGate;
      try {
        await route.fulfill(response);
      } catch {
        // Aborting the A request during logout is also a valid isolation path.
      } finally {
        notifyLateSettled();
      }
    });

    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/projects");
    await expect(page.getByText(projectName("A"))).toBeVisible();
    await followAppLink(page, "/search");
    const searchComposer = page.getByPlaceholder("Message NOUS…");
    await searchComposer.fill(marker);
    await searchComposer.press("Enter");
    await expect(page.getByText(searchAnswer("A"))).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Stop generation" }),
    ).toHaveCount(0);
    await searchComposer.fill(lateQuery);
    await searchComposer.press("Enter");
    await lateStarted;

    await signOutOnCurrentPage(page);
    account = "B";
    await signInOnCurrentPage(page, TEST_DATA.USERS.REGULAR);
    await followAppLink(page, "/search");
    await searchComposer.fill(marker);
    await searchComposer.press("Enter");
    await expect(page.getByText(searchAnswer("B"))).toBeVisible();
    releaseLate();
    await lateSettled;
    await expect(page.getByText(searchAnswer("A"))).toHaveCount(0);
    await expect(page.getByText(lateAnswer)).toHaveCount(0);
    await followAppLink(page, "/research");
    await expect(page.getByText(projectName("B"))).toBeVisible();
    await expect(page.getByText(projectName("A"))).toHaveCount(0);
  });

  test("a rejected A stream cannot leave private chat in B's browser", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    const canary = `iso-rejected-${Date.now()}`;
    await page.route(STREAM_URL, (route: Route) =>
      route.fulfill({
        status: 401,
        json: { detail: "Authentication required" },
      }),
    );

    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/chat?new=1");
    const composer = page.getByPlaceholder(
      "Ask anything, or paste a passage to discuss…",
    );
    await expect(composer).toBeVisible();
    await composer.fill(`Private ${canary}`);
    await page.getByRole("button", { name: /^Send/ }).click();
    await page.waitForURL(/\/login\?reauth=chat/, { timeout: 20000 });

    await signInOnCurrentPage(page, TEST_DATA.USERS.REGULAR);
    if (new URL(page.url()).pathname !== "/chat") {
      await followAppLink(page, "/chat");
    }
    await expect(composer).toBeVisible();
    await expect(page.getByText(canary)).toHaveCount(0);
    const persisted = await page.evaluate(
      () => window.localStorage.getItem("chat-storage") ?? "",
    );
    expect(persisted).not.toContain(canary);
  });
});
