import { expect, test, type Page, type TestInfo } from '@playwright/test';

const FIXTURE_PATH = '/visual-test/chat-mobile-controls';
const HISTORY_DOCK_BREAKPOINT = 1280;

async function openFixture(page: Page): Promise<void> {
  await page.route('**/api/v1/processing/jobs**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        jobs: [
          {
            id: 'job-1',
            job_type: 'arxiv_ingest',
            status: 'running',
            progress_percentage: 40,
            created_at: '2026-09-13T12:00:00.000Z',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    })
  );
  await page.goto(FIXTURE_PATH);
  await expect(page.getByTestId('chat-mobile-controls-fixture')).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Background jobs: 1 running' })
  ).toBeVisible();
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
}

async function pushPath(page: Page, pathname: string): Promise<void> {
  await page.evaluate((nextPath) => {
    window.history.pushState(null, '', nextPath);
  }, pathname);
  await expect(page.getByTestId('current-path')).toHaveText(pathname);
}

async function expectHeaderActionsInViewport(page: Page): Promise<string[]> {
  const actions = await page
    .getByTestId('chat-mobile-header')
    .locator('button:visible')
    .evaluateAll((buttons) =>
      buttons.map((button) => {
        const rect = button.getBoundingClientRect();
        return {
          name: button.getAttribute('aria-label') ?? button.textContent ?? '',
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
          width: rect.width,
          height: rect.height,
          viewportWidth: window.innerWidth,
          viewportHeight: window.innerHeight,
        };
      })
    );

  for (const action of actions) {
    expect.soft(action.width, `${action.name} width`).toBeGreaterThan(0);
    expect.soft(action.height, `${action.name} height`).toBeGreaterThan(0);
    expect
      .soft(action.left, `${action.name} left edge`)
      .toBeGreaterThanOrEqual(0);
    expect
      .soft(action.top, `${action.name} top edge`)
      .toBeGreaterThanOrEqual(0);
    expect
      .soft(action.right, `${action.name} right edge`)
      .toBeLessThanOrEqual(action.viewportWidth);
    expect
      .soft(action.bottom, `${action.name} bottom edge`)
      .toBeLessThanOrEqual(action.viewportHeight);
  }

  return actions.map((action) => action.name);
}

async function expectKeyboardReachability(
  page: Page,
  expectedNames: string[]
): Promise<void> {
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) {
      document.activeElement.blur();
    }
  });

  const focusOrder: string[] = [];
  const maxTabs = 60;
  for (let index = 0; index < maxTabs; index += 1) {
    await page.keyboard.press('Tab');
    const name = await page.evaluate(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement)) return '';
      return (
        active.getAttribute('aria-label') ?? active.textContent?.trim() ?? ''
      );
    });
    focusOrder.push(name);
    if (
      expectedNames.every((expectedName) => focusOrder.includes(expectedName))
    ) {
      break;
    }
  }

  for (const expectedName of expectedNames) {
    expect(focusOrder, `${expectedName} must be reachable with Tab`).toContain(
      expectedName
    );
  }
}

test('long chat controls remain reachable by pointer and keyboard', async ({
  page,
}, testInfo: TestInfo) => {
  await openFixture(page);
  await pushPath(page, '/chat');

  const actionNames = await expectHeaderActionsInViewport(page);
  const viewportWidth = page.viewportSize()?.width ?? 0;
  const expectedHeaderActions = [
    ...(viewportWidth < HISTORY_DOCK_BREAKPOINT ? ['Toggle chat history'] : []),
    'Background jobs: 1 running',
    'Copy all messages',
    'Export chat',
  ];
  expect(actionNames).toEqual(expectedHeaderActions);

  const title = page
    .getByTestId('chat-mobile-header')
    .locator('span[title]')
    .first();
  const titleLayout = await title.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      overflow: style.overflow,
      textOverflow: style.textOverflow,
      whiteSpace: style.whiteSpace,
    };
  });
  expect.soft(titleLayout.clientWidth).toBeGreaterThan(0);
  expect.soft(titleLayout.scrollWidth).toBeGreaterThan(titleLayout.clientWidth);
  expect.soft(titleLayout.overflow).toBe('hidden');
  expect.soft(titleLayout.textOverflow).toBe('ellipsis');
  expect.soft(titleLayout.whiteSpace).toBe('nowrap');

  const send = page.getByRole('button', { name: 'Send' });
  await page
    .getByRole('textbox', { name: 'Message' })
    .fill('Compare the cited studies and explain the recommendation.');
  await expect(send).toBeVisible();
  const sendCenterHitsSend = await send.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      box.x + box.width / 2,
      box.y + box.height / 2
    );
    return element === hit || element.contains(hit);
  });
  expect(sendCenterHitsSend).toBe(true);

  await page.screenshot({
    path: testInfo.outputPath('accepted-chat-controls.png'),
    fullPage: true,
  });

  await expectKeyboardReachability(page, [...expectedHeaderActions, 'Send']);
  await send.click();
  await expect(page.getByTestId('submission-count')).toHaveText(
    'Submissions: 1'
  );
});

test('launcher follows client-side routes and keeps the open-panel close control', async ({
  page,
}) => {
  await openFixture(page);
  const open = page.getByRole('button', { name: 'Open agent chat' });
  const close = page.getByRole('button', { name: 'Close agent chat' });

  await expect(open).toBeVisible();
  await pushPath(page, '/chat');
  await expect(open).toHaveCount(0);

  await pushPath(page, '/documents');
  await open.click();
  await expect(close).toBeVisible();

  await pushPath(page, '/chat');
  await expect(close).toBeVisible();
  await close.click();
  await expect(open).toHaveCount(0);

  await pushPath(page, '/documents');
  await expect(open).toBeVisible();
});
