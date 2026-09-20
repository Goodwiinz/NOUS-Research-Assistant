import { test as base, type BrowserContext } from '@playwright/test';
import { resolve } from 'node:path';
import { resolveAgentQaConfig } from '../../src/test/agent-qa/agentQaConfig';

type StorageState = Awaited<ReturnType<BrowserContext['storageState']>>;
const live = resolveAgentQaConfig(process.env, resolve(__dirname, '../..'));

export async function loginForAgentQa(
  context: BrowserContext,
  baseURL: string,
  credentials: { email: string; password: string }
): Promise<StorageState> {
  try {
    const page = await context.newPage();
    await page.goto('/login');
    await page
      .getByTestId('email-input')
      .fill(credentials.email, { timeout: 30_000 });
    await page.getByTestId('password-input').fill(credentials.password);
    await page.getByTestId('login-button').click();
    await page.waitForURL(
      (url) =>
        url.origin === baseURL && /^\/(dashboard|chat)(\/|$)/.test(url.pathname),
      { timeout: 30_000 }
    );
    return await context.storageState({ indexedDB: true });
  } catch {
    // Playwright action errors can include fill() arguments, including the
    // plaintext password. Never attach the original error or its cause.
    throw new Error('Agent Q&A login failed. Check credentials or login page availability.');
  } finally {
    await context.close();
  }
}

// Authenticate once outside recorded test contexts. Fresh contexts still isolate
// every dataset case; the login form and password never enter test recordings.
export const test = base.extend<
  object,
  { agentQaSession: string | StorageState }
>({
  agentQaSession: [
    async ({ browser }, provide) => {
      if (!live.enabled) throw new Error('Live agent Q&A is disabled.');
      if ('storageState' in live.auth) {
        await provide(live.auth.storageState);
        return;
      }
      const context = await browser.newContext({ baseURL: live.baseURL });
      const state = await loginForAgentQa(context, live.baseURL, live.auth);
      await provide(state);
    },
    { scope: 'worker' },
  ],
  storageState: async ({ agentQaSession }, provide) => {
    await provide(agentQaSession);
  },
});
