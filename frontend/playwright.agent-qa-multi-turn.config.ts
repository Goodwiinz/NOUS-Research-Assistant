import { defineConfig } from '@playwright/test';
import agentQaConfig from './playwright.agent-qa.config';

export default defineConfig(agentQaConfig, {
  testMatch: 'live-agent-qa-multi-turn.spec.ts',
});
