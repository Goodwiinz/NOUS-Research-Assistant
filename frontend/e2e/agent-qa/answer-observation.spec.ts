import { expect, test } from '@playwright/test';
import {
  observeCommittedAnswer,
  observeNextCommittedAnswer,
} from './answerObservation';

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

test('waits for a new committed answer instead of reading the previous turn', async ({
  page,
}) => {
  await page.setContent(
    '<div data-runtime-id="first"><article data-role="assistant"><div data-quotable>Earlier answer</div></article></div>'
  );
  const rows = page.locator('[data-runtime-id]').filter({
    has: page.locator('[data-role="assistant"] [data-quotable]'),
  });
  const next = observeNextCommittedAnswer(rows, 'first', 1_000);
  await page.evaluate(() => {
    window.setTimeout(() => {
      document.body.insertAdjacentHTML(
        'beforeend',
        '<div data-runtime-id="second"><article data-role="assistant"><div data-quotable>Current answer</div></article></div>'
      );
    }, 50);
  });

  await expect(next).resolves.toEqual({
    actualAnswer: 'Current answer',
    phase: 'sent',
    runtimeId: 'second',
  });
});

test('does not reuse the previous answer when the next turn never commits', async ({
  page,
}) => {
  await page.setContent(
    '<div data-runtime-id="first"><article data-role="assistant"><div data-quotable>Earlier answer</div></article></div>'
  );
  const rows = page.locator('[data-runtime-id]').filter({
    has: page.locator('[data-role="assistant"] [data-quotable]'),
  });

  await expect(
    observeNextCommittedAnswer(rows, 'first', 200)
  ).rejects.toThrow();
});
