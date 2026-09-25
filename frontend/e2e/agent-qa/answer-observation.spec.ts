import { expect, test } from '@playwright/test';
import { observeCommittedAnswer } from './answerObservation';

const answer = '[data-role="assistant"] [data-quotable]';

test('classifies an empty ordered-list marker as rendered-empty', async ({
  page,
}) => {
  await page.setContent(
    '<article data-role="assistant"><div data-quotable><ol start="2022"><li></li></ol></div></article>'
  );

  await expect(
    observeCommittedAnswer(page.locator(answer).last(), 200)
  ).resolves.toEqual({ actualAnswer: '', phase: 'rendered-empty' });
});

test('reads selectable committed answer text', async ({ page }) => {
  await page.setContent(
    '<article data-role="assistant"><div data-quotable><p>2022.</p></div></article>'
  );

  await expect(
    observeCommittedAnswer(page.locator(answer).last(), 200)
  ).resolves.toEqual({ actualAnswer: '2022.', phase: 'sent' });
});

test('keeps the turn sent when no committed answer arrives', async ({
  page,
}) => {
  await page.setContent('<article data-role="assistant"></article>');
  await expect(
    observeCommittedAnswer(page.locator(answer).last(), 200)
  ).rejects.toThrow();
});
