import { defineConfig } from '@playwright/test';

// Self-contained synthetic fixtures and routed local assets; no backend/login.
export default defineConfig({
  testDir: './e2e/artifacts',
  testMatch: 'pdf-preview.spec.ts',
  timeout: 30_000,
  workers: 1,
  use: { browserName: 'chromium', channel: 'chromium', headless: true },
});
