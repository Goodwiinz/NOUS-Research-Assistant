import { defineConfig, devices } from '@playwright/test';
import {
  assertAgentQaRunLocationAvailable,
  resolveAgentQaConfig,
  resolveAgentQaReportPaths,
} from './src/test/agent-qa/agentQaConfig';

const live = resolveAgentQaConfig(process.env, __dirname);
const reportPaths = resolveAgentQaReportPaths(process.env);
assertAgentQaRunLocationAvailable(reportPaths, process.env, __dirname);

export default defineConfig({
  testDir: './e2e/agent-qa',
  testMatch: 'live-agent-qa.spec.ts',
  outputDir: reportPaths.outputDir,
  timeout: 300_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: reportPaths.htmlOutputFolder }],
    ['json', { outputFile: reportPaths.jsonOutputFile }],
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
