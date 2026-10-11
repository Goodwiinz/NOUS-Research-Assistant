import { defineConfig } from '@playwright/test';

// Production components, routed synthetic transport and explicit fixture identity.
// This configuration starts no application server and bypasses no parent CSP.
export default defineConfig({
  testDir: './e2e/artifacts',
  testMatch: 'sharing.spec.ts',
  outputDir: '/tmp/rag-completion-sharing-browser-results',
  timeout: 30_000,
  workers: 1,
  retries: 0,
  use: {
    browserName: 'chromium',
    headless: true,
    bypassCSP: false,
    permissions: ['clipboard-read', 'clipboard-write'],
    launchOptions: { args: ['--no-sandbox'] },
  },
});
