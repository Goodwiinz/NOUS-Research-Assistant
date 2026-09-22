import { defineConfig, devices } from 'playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 12 * 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0, // A retry could leave another real project behind.
  outputDir: './test-results',
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: process.env.NOUS_BASE_URL ?? 'https://goodwiinz.tech',
    storageState: process.env.NOUS_AUTH_STATE ?? '.auth/nous.json',
    viewport: { width: 1440, height: 1000 },
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: { mode: 'on', size: { width: 1440, height: 1000 } },
  },
  projects: [{ name: 'chromium' }],
});
