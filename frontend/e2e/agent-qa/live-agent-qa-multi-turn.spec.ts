import { expect } from '@playwright/test';
import { test } from './live-test';
import { observeNextCommittedAnswer } from './answerObservation';
import { interleaveAgentQaCases } from './multiTurnSequence';

import {
  gradeAgentAnswer,
  type AgentAnswerCriteria,
  type AgentAnswerGrade,
} from '../../src/test/agent-qa/agentAnswerGrader';
import rawDataset from '../fixtures/agent-qa.v1.json';
import rawFollowUps from '../fixtures/agent-qa.multi-turn.v1.json';

interface AgentQaCase {
  id: string;
  category: string;
  question: string;
  referenceAnswer: string;
  criteria: AgentAnswerCriteria;
}

interface FollowUpCase extends AgentQaCase {
  afterCaseId: string;
}

interface TurnResult {
  turn: number;
  source: 'baseline' | 'follow-up';
  id: string;
  category: string;
  question: string;
  referenceAnswer: string;
  phase: 'not-sent' | 'sent' | 'rendered-empty' | 'graded';
  actualAnswer?: string;
  grade?: AgentAnswerGrade;
  elapsedMs?: number;
  threadUrl?: string;
}

const dataset = rawDataset as { version: string; cases: AgentQaCase[] };
const followUps = rawFollowUps as { version: string; cases: FollowUpCase[] };
const cases = interleaveAgentQaCases(dataset.cases, followUps.cases);
const turnTimeout = 240_000;

test.skip(
  process.env.AGENT_QA_LIVE !== '1',
  'Set AGENT_QA_LIVE=1 to run live agent Q&A.'
);

test('asks the full Q&A suite and contextual follow-ups in one chat', async ({
  page,
}) => {
  test.setTimeout(cases.length * turnTimeout + 90_000);
  test.info().annotations.push({
    type: 'dataset',
    description: `agent-qa.v1.json@${dataset.version} + agent-qa.multi-turn.v1.json@${followUps.version}`,
  });

  const results: TurnResult[] = [];
  let threadId: string | null = null;
  try {
    // Wait for the existing chat to finish restoring before starting a fresh
    // conversation. Otherwise its pending route update may reclaim the URL.
    const initialThreadsResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        /\/api\/v2\/conversations\/[^/]+\/threads(?:\?|$)/.test(response.url()),
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
    const restoredThread = page
      .getByRole('complementary', { name: 'Chat context rail' })
      .getByText(/^thread · [0-9a-f]{8}$/);
    await expect
      .poll(
        async () =>
          new URL(page.url()).searchParams.has('thread') ||
          (await welcome.isVisible()) ||
          (await restoredThread.isVisible()),
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

    const committedAnswerRows = page.locator('[data-runtime-id]').filter({
      has: page.locator('[data-role="assistant"] [data-quotable]'),
    });
    for (const [index, { source, testCase }] of cases.entries()) {
      const result: TurnResult = {
        turn: index + 1,
        source,
        id: testCase.id,
        category: testCase.category,
        question: testCase.question,
        referenceAnswer: testCase.referenceAnswer,
        phase: 'not-sent',
      };
      results.push(result);

      await test.step(`turn ${result.turn}: ${testCase.id}`, async () => {
        const previousAnswerId =
          (await committedAnswerRows.count()) > 0
            ? await committedAnswerRows.last().getAttribute('data-runtime-id')
            : null;
        const startedAt = Date.now();
        await composer.fill(testCase.question);
        await page.getByRole('button', { name: /^Send/ }).click();
        result.phase = 'sent';
        await expect(page.locator('[data-role="user"]').last()).toContainText(
          testCase.question
        );

        // Identity, rather than `.last()` alone, ensures this turn never
        // mistakes the prior committed reply for its own answer.
        const observation = await observeNextCommittedAnswer(
          committedAnswerRows,
          previousAnswerId,
          turnTimeout
        );
        result.phase = observation.phase;
        result.actualAnswer = observation.actualAnswer;
        result.elapsedMs = Date.now() - startedAt;
        expect(
          result.actualAnswer,
          'committed answer has no selectable text'
        ).toMatch(/\S/);
        await expect(
          page.getByRole('button', { name: 'Stop agent' })
        ).toHaveCount(0);
        await expect(
          page.getByRole('alertdialog', { name: 'Approval needed' })
        ).toHaveCount(0);
        await expect(
          page.locator('[data-role="assistant"] [role="alert"]')
        ).toHaveCount(0);

        await expect
          .poll(() => Boolean(new URL(page.url()).searchParams.get('thread')), {
            timeout: 30_000,
          })
          .toBe(true);
        const currentThreadId = new URL(page.url()).searchParams.get('thread');
        if (threadId === null) threadId = currentThreadId;
        expect(currentThreadId, 'a turn moved to a different chat thread').toBe(
          threadId
        );
        result.threadUrl = page.url();

        result.grade = gradeAgentAnswer(result.actualAnswer, testCase.criteria);
        result.phase = 'graded';
        await test.info().attach(`turn-${result.turn}-result`, {
          body: JSON.stringify(result, null, 2),
          contentType: 'application/json',
        });
        // A rubric mismatch should not prevent later turns from exercising
        // memory, correction, and conversation continuity.
        expect
          .soft(result.grade.passed, JSON.stringify(result, null, 2))
          .toBe(true);
      });
    }
  } finally {
    await test.info().attach('agent-qa-multi-turn-results', {
      body: JSON.stringify(
        {
          baselineVersion: dataset.version,
          followUpVersion: followUps.version,
          threadId,
          results,
        },
        null,
        2
      ),
      contentType: 'application/json',
    });
  }
});
