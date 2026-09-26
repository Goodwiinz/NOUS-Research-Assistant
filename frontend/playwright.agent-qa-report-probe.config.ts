import { defineConfig, devices } from '@playwright/test';
import {
  assertAgentQaRunLocationAvailable,
  resolveAgentQaReportPaths,
} from './src/test/agent-qa/agentQaConfig';

// Exercises the live report-path lifecycle in real Playwright workers without
// authentication, remote requests, or agent turns.
const paths = resolveAgentQaReportPaths({ ...process.env, AGENT_QA_LIVE: '1' });
assertAgentQaRunLocationAvailable(
  paths,
  { ...process.env, AGENT_QA_LIVE: '1' },
  __dirname
);

export default defineConfig({
  testDir: './e2e/agent-qa',
  testMatch: 'answer-observation.spec.ts',
  outputDir: paths.outputDir,
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: paths.htmlOutputFolder }],
    ['json', { outputFile: paths.jsonOutputFile }],
  ],
  projects: [{ name: 'chromium', use: devices['Desktop Chrome'] }],
});
