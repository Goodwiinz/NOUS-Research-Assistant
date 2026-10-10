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
 * The project, search and rejected-stream cases name the guard they gate and
 * the mutation that turns them red.
 */

const STREAM_URL = "**/api/v1/agent/stream";
const RESUME_URL = "**/agent/stream/resume/**";
const PROJECTS_URL = "**/api/v1/projects*";
const SEARCH_URL = "**/api/v1/search/hybrid";
// frontend/src/hooks/chat/chatAuthRecovery.ts CHAT_AUTH_RECOVERY_STORAGE_KEY
const CHAT_AUTH_RECOVERY_KEY = "nous:chat-auth-recovery:v1";

type Credentials = { email: string; password: string };

async function expectSignedInAs(
  page: Page,
  credentials: Credentials,
): Promise<void> {
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible({
    timeout: 20000,
  });
  await expect(page.getByRole("link", { name: "Account" })).toContainText(
    credentials.email.split("@")[0].slice(0, 2).toUpperCase(),
    { timeout: 20000 },
  );
}

async function signInOnCurrentPage(
  page: Page,
  credentials: Credentials,
): Promise<void> {
  await expect(page.getByTestId("email-input")).toBeVisible();
  await page.getByTestId("email-input").fill(credentials.email);
  await page.getByTestId("password-input").fill(credentials.password);
  await page.getByTestId("login-button").click();
  await expectSignedInAs(page, credentials);
}

async function signOutOnCurrentPage(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL((url) => url.pathname === "/login");
}

async function followAppLink(page: Page, path: string): Promise<void> {
  await page.locator(`a[href="${path}"]`).first().click();
  await page.waitForURL((url) => url.pathname === path);
}

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
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
    let agent = page.getByRole("complementary", { name: "Agent chat sidebar" });
    await agent.getByPlaceholder("Ask the agent...").fill(marker);
    await agent.getByRole("button", { name: "Send message" }).click();
    await expect(agent.getByText(answer("A"))).toBeVisible();
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
    agent = page.getByRole("complementary", { name: "Agent chat sidebar" });
    await expect(agent.getByText(answer("A"))).toHaveCount(0);
    await agent.getByPlaceholder("Ask the agent...").fill(`B-${marker}`);
    await agent.getByRole("button", { name: "Send message" }).click();
    await expect(agent.getByText(answer("B"))).toBeVisible();
    await expect(agent.getByText(answer("A"))).toHaveCount(0);
  });

  // Guard: clearUserScopedClientState resets useProjectStore at sign-out
  // (frontend/src/stores/authStore.ts:125). B's project fetch fails here, so
  // ProjectsPage keeps whatever the store held: without the reset that is A's
  // project, shown to B next to the error. Mutation: delete that reset and
  // this test fails on the "A's project is absent" check. A successful B fetch
  // cannot catch it: holding the response only shows the loading skeleton,
  // and once it lands B's list replaces A's.
  // Focused: pnpm --dir tests/e2e exec playwright test tests/account-switch-isolation.spec.ts --project=chromium -g "project list"
  test("B's project list never shows A's project when B's fetch fails", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    const marker = `iso-project-${Date.now()}`;
    let account: "A" | "B" = "A";
    const projectName = (owner: "A" | "B") => `${owner}-project-${marker}`;

    await page.route(PROJECTS_URL, (route: Route) => {
      if (
        route.request().method() !== "GET" ||
        new URL(route.request().url()).pathname !== "/api/v1/projects"
      ) {
        return route.continue();
      }
      if (account === "B") {
        return route.fulfill({
          status: 500,
          json: { detail: "Synthetic project list failure" },
        });
      }
      return route.fulfill({
        json: {
          projects: [
            {
              id: "11111111-1111-4111-8111-111111111111",
              workspace_id: "33333333-3333-4333-8333-333333333333",
              name: projectName("A"),
              description: "Synthetic A project",
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

    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/projects");
    await expect(page.getByText(projectName("A"))).toBeVisible();
    // Sign out from another page: an unauthenticated ProjectsPage resets the
    // store itself, which would hide a missing sign-out reset.
    await followAppLink(page, "/search");
    await expect(page.getByText(projectName("A"))).toHaveCount(0);

    await signOutOnCurrentPage(page);
    account = "B";
    await signInOnCurrentPage(page, TEST_DATA.USERS.REGULAR);
    await followAppLink(page, "/research");
    // The failed fetch has settled once its error banner renders.
    await expect(
      page
        .getByRole("alert")
        .filter({ has: page.getByRole("button", { name: "Dismiss" }) }),
    ).toBeVisible({ timeout: 20000 });
    await expect(page.getByText(projectName("A"))).toHaveCount(0);
    await expect(page.getByText("No projects yet")).toBeVisible();
  });

  // Guard: AuthProvider remounts the app under `key: accountRevision`
  // (frontend/src/hooks/useAuth.tsx:115), discarding page-local state such as
  // SearchPage's messages; the API client also aborts A's request with the
  // account signal. A same-tab sign-out cannot test this: leaving /search
  // unmounts the page anyway. Here another tab switches the account, so
  // /search stays mounted. Mutation: drop `key: accountRevision` and this test
  // fails because A's earlier answer is still on screen for B.
  // Focused: pnpm --dir tests/e2e exec playwright test tests/account-switch-isolation.spec.ts --project=chromium -g "held A search"
  test("a held A search cannot land in a tab that another tab switched to B", async ({
    page,
    context,
  }, testInfo) => {
    const helpers = createTestHelpers(page, context, testInfo);
    const marker = `iso-search-${Date.now()}`;
    const lateQuery = `late-${marker}`;
    const lateAnswer = `A-late-search-${marker}`;
    let account: "A" | "B" = "A";
    const searchAnswer = (owner: "A" | "B") => `${owner}-search-${marker}`;
    const lateStarted = deferred();
    const lateGate = deferred();
    const lateSettled = deferred();

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
      lateStarted.resolve();
      await lateGate.promise;
      try {
        await route.fulfill(response);
      } catch {
        // The account signal aborted the request; that is the guarded path.
      } finally {
        lateSettled.resolve();
      }
    });

    await helpers.login(TEST_DATA.USERS.ADMIN);
    await helpers.navigateTo("/search");
    const searchComposer = page.getByPlaceholder("Message NOUS…");
    await searchComposer.fill(marker);
    await searchComposer.press("Enter");
    await expect(page.getByText(searchAnswer("A"))).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Stop generation" }),
    ).toHaveCount(0);
    await searchComposer.fill(lateQuery);
    await searchComposer.press("Enter");
    await lateStarted.promise;

    // Same browser, second tab: A signs out and B signs in. Supabase relays
    // both events to the first tab, which never navigates.
    const otherTab = await context.newPage();
    await otherTab.goto("/dashboard");
    await expectSignedInAs(otherTab, TEST_DATA.USERS.ADMIN);
    await signOutOnCurrentPage(otherTab);
    account = "B";
    await signInOnCurrentPage(otherTab, TEST_DATA.USERS.REGULAR);

    await expectSignedInAs(page, TEST_DATA.USERS.REGULAR);
    await otherTab.close();
    expect(new URL(page.url()).pathname).toBe("/search");
    await expect(page.getByText(searchAnswer("A"))).toHaveCount(0);

    lateGate.resolve();
    await lateSettled.promise;
    // B's own round trip renders after A's late response was delivered, so
    // the absence checks below run after any late render would have landed.
    await searchComposer.fill(marker);
    await searchComposer.press("Enter");
    await expect(page.getByText(searchAnswer("B"))).toBeVisible();
    await expect(page.getByText(lateAnswer)).toHaveCount(0);
    await expect(page.getByText(searchAnswer("A"))).toHaveCount(0);
  });

  // Guard: observeIdentity discards a chat recovery draft owned by another
  // account (frontend/src/stores/authStore.ts:173). The chat route only
  // consumes a draft after it reopens the owner's thread, which B never can,
  // so without the guard A's prompt stays in B's sessionStorage for up to
  // 15 minutes. Mutation: delete that call and this test fails on the
  // sessionStorage check. Unit counterpart:
  // frontend/src/store/__tests__/auth-account-isolation.test.ts ("chat draft").
  // Focused: pnpm --dir tests/e2e exec playwright test tests/account-switch-isolation.spec.ts --project=chromium -g "rejected A stream"
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
    const readRecovery = () =>
      page.evaluate(
        (key) => window.sessionStorage.getItem(key) ?? "",
        CHAT_AUTH_RECOVERY_KEY,
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
    // Positive control: the 401 recovery staged A's prompt for A's re-login,
    // so the check after B signs in reads the key that actually held it.
    const staged = await readRecovery();
    expect(staged).toContain(canary);
    const stagedThread = (JSON.parse(staged) as { threadId: string }).threadId;

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
    const recovery = await readRecovery();
    expect(recovery).not.toContain(canary);
    expect(recovery).not.toContain(stagedThread);
  });
});
