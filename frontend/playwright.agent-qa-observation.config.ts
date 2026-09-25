import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/agent-qa',
  testMatch: 'answer-observation.spec.ts',
  outputDir: './test-results/agent-qa-observation',
  timeout: 10_000,
  reporter: [['list']],
  projects: [
    {
      name: 'chromium',
      use: devices['Desktop Chrome'],
    },
  ],
});
