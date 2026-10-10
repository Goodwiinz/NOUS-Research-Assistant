import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/artifacts',
  testMatch: 'html-isolation.spec.ts',
  workers: 1,
  retries: 0,
  use: {
    browserName: 'chromium',
    headless: true,
    bypassCSP: false,
    launchOptions: { args: ['--no-sandbox'] },
  },
});
