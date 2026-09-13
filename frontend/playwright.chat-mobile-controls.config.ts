import { defineConfig, devices } from '@playwright/test';

const PORT = 3107;
const baseURL = `http://127.0.0.1:${PORT}`;
const outputDir =
  process.env.CHAT_MOBILE_TEST_OUTPUT_DIR ??
  './test-results/chat-mobile-controls';

const viewports = [
  { name: '320', viewport: { width: 320, height: 844 } },
  { name: '375', viewport: { width: 375, height: 844 } },
  { name: '390', viewport: { width: 390, height: 844 } },
  { name: '768', viewport: { width: 768, height: 844 } },
  { name: '1280', viewport: { width: 1280, height: 900 } },
];

export default defineConfig({
  testDir: './e2e/visual',
  testMatch: 'chat-mobile-controls.spec.ts',
  outputDir,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  expect: { timeout: 10_000 },
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    colorScheme: 'light',
    deviceScaleFactor: 1,
    trace: 'retain-on-failure',
  },
  projects: viewports.map(({ name, viewport }) => ({
    name: `chromium-${name}`,
    use: { viewport },
  })),
  webServer: {
    command: `env NEXT_PUBLIC_VISUAL_TEST_FIXTURES=1 NEXT_PUBLIC_API_BASE_URL=${baseURL}/api/v1 NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321 NEXT_PUBLIC_SUPABASE_ANON_KEY=visual-test-anon-key corepack pnpm@10.18.2 exec next dev --hostname 127.0.0.1 --port ${PORT}`,
    url: `${baseURL}/visual-test/chat-mobile-controls`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
