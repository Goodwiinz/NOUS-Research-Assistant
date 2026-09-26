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
