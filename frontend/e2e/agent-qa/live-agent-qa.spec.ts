import { expect } from '@playwright/test';
import { test } from './live-test';

import {
  gradeAgentAnswer,
  type AgentAnswerCriteria,
} from '../../src/test/agent-qa/agentAnswerGrader';
import rawDataset from '../fixtures/agent-qa.v1.json';

interface AgentQaCase {
  id: string;
  category: string;
  question: string;
  referenceAnswer: string;
  criteria: AgentAnswerCriteria;
}

const dataset = rawDataset as {
  version: string;
  description: string;
  cases: AgentQaCase[];
};

const liveEnabled = process.env.AGENT_QA_LIVE === '1';
const turnTimeout = 240_000;

test.skip(!liveEnabled, 'Set AGENT_QA_LIVE=1 to run live agent Q&A.');

test.describe(`live agent Q&A dataset v${dataset.version}`, () => {
  for (const testCase of dataset.cases) {
    test(`${testCase.id} [${testCase.category}]`, async ({ page }) => {
      test.info().annotations.push({
        type: 'dataset',
        description: `agent-qa.v1.json#${testCase.id}`,
      });
      await test.info().attach('expected-case', {
        body: JSON.stringify(
          {
            datasetVersion: dataset.version,
            id: testCase.id,
            category: testCase.category,
            question: testCase.question,
            referenceAnswer: testCase.referenceAnswer,
            criteria: testCase.criteria,
          },
          null,
          2
        ),
        contentType: 'application/json',
      });

      // Let the existing session finish restoring before requesting a new
      // chat. A direct ?new=1 navigation can race the parallel workspace and
      // conversation initializers, leaving the first send on an old thread.
      const initialThreadsResponse = page.waitForResponse(
        (response) =>
          response.request().method() === 'GET' &&
          /\/api\/v2\/conversations\/[^/]+\/threads(?:\?|$)/.test(
            response.url()
          ),
        { timeout: 30_000 }
      );
      await page.goto('/chat');
      const threadsResponse = await initialThreadsResponse;
      await threadsResponse.finished();
      expect(threadsResponse.ok()).toBe(true);
      const composer = page.getByPlaceholder(/Ask anything/);
      await expect(composer).toBeEnabled({ timeout: 30_000 });
      const welcome = page.getByRole('heading', {
        name: 'What would you like to find out?',
      });
      await expect
        .poll(
          async () =>
            new URL(page.url()).searchParams.has('thread') ||
            (await welcome.isVisible()),
          { timeout: 30_000 }
        )
        .toBe(true);

      await page.getByRole('button', { name: /^New chat/ }).click();
      await expect(welcome).toBeVisible();
      await expect
        .poll(() => {
          const params = new URL(page.url()).searchParams;
          return params.get('new') === '1' && !params.has('thread');
        })
        .toBe(true);
      await expect(page.locator('[data-role="user"]')).toHaveCount(0);
      await expect(page.locator('[data-role="assistant"]')).toHaveCount(0);

      const startedAt = Date.now();
      await composer.fill(testCase.question);
      await page.getByRole('button', { name: /^Send/ }).click();

      await expect(page.locator('[data-role="user"]').last()).toContainText(
        testCase.question
      );

      // Only completed assistant prose has data-quotable. Streaming content,
      // progress indicators, tool output, and citation chrome are excluded.
      const committedAnswer = page
        .locator('[data-role="assistant"] [data-quotable]')
        .last();
      await expect(committedAnswer).toHaveText(/\S/, { timeout: turnTimeout });
      await expect(
        page.getByRole('button', { name: 'Stop agent' })
      ).toHaveCount(0);
      await expect(
        page.getByRole('alertdialog', { name: 'Approval needed' })
      ).toHaveCount(0);
      await expect(
        page.locator('[data-role="assistant"] [role="alert"]')
      ).toHaveCount(0);

      const actualAnswer = (await committedAnswer.innerText()).trim();
      const grade = gradeAgentAnswer(actualAnswer, testCase.criteria);
      const result = {
        datasetVersion: dataset.version,
        id: testCase.id,
        category: testCase.category,
        question: testCase.question,
        referenceAnswer: testCase.referenceAnswer,
        actualAnswer,
        grade,
        elapsedMs: Date.now() - startedAt,
        threadUrl: page.url(),
      };

      await test.info().attach('agent-qa-result', {
        body: JSON.stringify(result, null, 2),
        contentType: 'application/json',
      });

      expect(grade.passed, JSON.stringify(result, null, 2)).toBe(true);
    });
  }
});
