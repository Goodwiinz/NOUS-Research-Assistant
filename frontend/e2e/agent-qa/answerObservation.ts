import { expect, type Locator } from '@playwright/test';

export async function observeCommittedAnswer(
  committedAnswer: Locator,
  timeout: number
): Promise<{ actualAnswer: string; phase: 'sent' | 'rendered-empty' }> {
  await expect(committedAnswer).toHaveCount(1, { timeout });
  // textContent excludes CSS list markers, including a bare year that
  // Markdown parsed as an empty ordered-list item.
  const actualAnswer = (await committedAnswer.textContent())?.trim() ?? '';
  return {
    actualAnswer,
    phase: actualAnswer ? 'sent' : 'rendered-empty',
  };
}

export async function observeNextCommittedAnswer(
  committedAnswerRows: Locator,
  previousRuntimeId: string | null,
  timeout: number
): Promise<{
  actualAnswer: string;
  phase: 'sent' | 'rendered-empty';
  runtimeId: string | null;
}> {
  const row = committedAnswerRows.last();
  await expect
    .poll(
      async () => {
        if ((await committedAnswerRows.count()) === 0) return false;
        const runtimeId = await row.getAttribute('data-runtime-id', {
          timeout: Math.min(timeout, 1_000),
        });
        return Boolean(runtimeId) && runtimeId !== previousRuntimeId;
      },
      { timeout }
    )
    .toBe(true);
  const observation = await observeCommittedAnswer(
    row.locator('[data-role="assistant"] [data-quotable]'),
    timeout
  );
  return {
    ...observation,
    runtimeId: await row.getAttribute('data-runtime-id'),
  };
}
