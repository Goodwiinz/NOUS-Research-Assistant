import { defineConfig, devices } from '@playwright/test';
import { resolveAgentQaConfig } from './src/test/agent-qa/agentQaConfig';

const live = resolveAgentQaConfig(process.env, __dirname);

export default defineConfig({
  testDir: './e2e/agent-qa',
  testMatch: 'live-agent-qa.spec.ts',
  outputDir: './test-results/agent-qa',
  timeout: 300_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: 'playwright-agent-qa-report' }],
    ['json', { outputFile: 'test-results/agent-qa/results.json' }],
  ],
  use: {
    baseURL: live.enabled ? live.baseURL : undefined,
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 800 },
      },
    },
  ],
});
