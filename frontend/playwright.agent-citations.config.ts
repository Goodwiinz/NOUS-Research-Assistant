import { defineConfig, devices } from '@playwright/test';

const PORT = 3107;
const baseURL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: './e2e/visual',
  testMatch: 'agent-citations-workspace.spec.ts',
  outputDir: './test-results/agent-citations',
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  expect: { timeout: 10_000 },
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    viewport: { width: 1280, height: 900 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `env NEXT_PUBLIC_VISUAL_TEST_FIXTURES=1 NEXT_PUBLIC_API_BASE_URL=${baseURL}/api/v1 NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321 NEXT_PUBLIC_SUPABASE_ANON_KEY=visual-test-anon-key corepack pnpm@10.18.2 exec next dev --hostname 127.0.0.1 --port ${PORT}`,
    url: `${baseURL}/visual-test/agent-citations?new=1`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
