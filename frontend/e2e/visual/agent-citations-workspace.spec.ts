import { expect, test, type Page, type Request } from '@playwright/test';

const WORKSPACE_ID = '11111111-1111-4111-8111-111111111111';
const CREATED_THREAD_ID = '22222222-2222-4222-8222-222222222222';
const CREATED_CONVERSATION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const ASSISTANT_MESSAGE_ID = 'abababab-abab-4bab-8bab-abababababab';
const USER_MESSAGE_ID = 'bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc';
const ANSWER =
  'Alpha evidence [Doc 1], beta evidence [Doc 2], another alpha chunk [Doc 3], gamma evidence [Doc 4], and delta evidence [Doc 5].';

const citations = [
  {
    document_id: 'c1c1c1c1-c1c1-41c1-81c1-c1c1c1c1c1c1',
    title: 'Alpha paper',
    content: 'Alpha first passage',
    score: 0.95,
    source_position: 1,
    chunk_id: 'alpha-1',
    chunk_index: 0,
    page_number: 3,
  },
  {
    document_id: 'd2d2d2d2-d2d2-42d2-82d2-d2d2d2d2d2d2',
    title: 'Beta paper',
    content: 'Beta passage',
    score: 0.9,
    source_position: 2,
    chunk_id: 'beta-1',
    chunk_index: 1,
    page_number: 8,
  },
  {
    document_id: 'c1c1c1c1-c1c1-41c1-81c1-c1c1c1c1c1c1',
    title: 'Alpha paper',
    content: 'Alpha second passage',
    score: 0.88,
    source_position: 3,
    chunk_id: 'alpha-2',
    chunk_index: 2,
    page_number: 4,
  },
  {
    document_id: 'e3e3e3e3-e3e3-43e3-83e3-e3e3e3e3e3e3',
    title: 'Gamma paper',
    content: 'Gamma passage',
    score: 0.84,
    source_position: 4,
    chunk_id: 'gamma-1',
    chunk_index: 3,
    page_number: 13,
  },
  {
    document_id: 'f4f4f4f4-f4f4-44f4-84f4-f4f4f4f4f4f4',
    title: 'Delta paper',
    content: 'Delta passage',
    score: 0.8,
    source_position: 5,
    chunk_id: 'delta-1',
    chunk_index: 4,
    page_number: 21,
  },
];

function thread(
  id: string,
  conversationId: string,
  title: string
): Record<string, unknown> {
  return {
    id,
    conversation_id: conversationId,
    title,
    status: 'active',
    last_message_at: '2026-09-12T12:00:00Z',
    last_message_preview: `${title} preview`,
    message_count: 2,
    token_count: 20,
    created_at: '2026-09-12T11:00:00Z',
    updated_at: '2026-09-12T12:00:00Z',
  };
}

async function installApiFixture(page: Page): Promise<{
  getStreamRequest: () => Request | null;
  releaseCanonicalMessages: () => void;
}> {
  let created = false;
  let streamRequest: Request | null = null;
  let releaseCanonicalMessages!: () => void;
  const canonicalMessagesGate = new Promise<void>((resolve) => {
    releaseCanonicalMessages = resolve;
  });
  const workspace = {
    id: WORKSPACE_ID,
    name: 'Citation workspace',
    description: 'Browser regression fixture',
    is_public: false,
    owner_id: '12121212-1212-4212-8212-121212121212',
    member_count: 1,
    collection_count: 0,
    conversation_count: 4,
    created_at: '2026-09-12T10:00:00Z',
    updated_at: '2026-09-12T10:00:00Z',
  };

  await page.route('**/api/v2/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (request.method() === 'GET' && url.pathname === '/api/v2/workspaces') {
      await route.fulfill({ json: [workspace] });
      return;
    }
    if (
      request.method() === 'GET' &&
      url.pathname === `/api/v2/workspaces/${WORKSPACE_ID}`
    ) {
      await route.fulfill({ json: workspace });
      return;
    }
    if (
      request.method() === 'GET' &&
      url.pathname === `/api/v2/workspaces/${WORKSPACE_ID}/threads`
    ) {
      const pageNumber = Number(url.searchParams.get('page') ?? 1);
      const pageThreads =
        pageNumber === 1
          ? [
              ...(created
                ? [
                    thread(
                      CREATED_THREAD_ID,
                      CREATED_CONVERSATION_ID,
                      'New grounded answer'
                    ),
                  ]
                : []),
              thread(
                '33333333-3333-4333-8333-333333333333',
                '77777777-7777-4777-8777-777777777777',
                'Research thread'
              ),
              thread(
                '44444444-4444-4444-8444-444444444444',
                '88888888-8888-4888-8888-888888888888',
                'Writing thread'
              ),
            ]
          : [
              thread(
                '44444444-4444-4444-8444-444444444444',
                '88888888-8888-4888-8888-888888888888',
                'Writing thread updated'
              ),
              thread(
                '55555555-5555-4555-8555-555555555555',
                '99999999-9999-4999-8999-999999999999',
                'Archive thread'
              ),
            ];
      await route.fulfill({
        json: {
          threads: pageThreads,
          total: created ? 4 : 3,
          page: pageNumber,
          limit: 50,
          has_more: pageNumber === 1,
        },
      });
      return;
    }
    if (
      request.method() === 'GET' &&
      url.pathname === `/api/v2/workspaces/${WORKSPACE_ID}/conversations`
    ) {
      await route.fulfill({
        json: {
          conversations: [
            {
              id: '66666666-6666-4666-8666-666666666666',
              workspace_id: WORKSPACE_ID,
              title: 'Default conversation',
              created_at: '2026-09-12T10:00:00Z',
              updated_at: '2026-09-12T10:00:00Z',
            },
          ],
          total: 1,
          page: 1,
          limit: 1,
          has_more: false,
        },
      });
      return;
    }
    if (request.method() === 'POST' && url.pathname === '/api/v2/threads') {
      created = true;
      await route.fulfill({
        json: thread(
          CREATED_THREAD_ID,
          CREATED_CONVERSATION_ID,
          'New grounded answer'
        ),
      });
      return;
    }
    if (
      request.method() === 'GET' &&
      url.pathname === `/api/v2/threads/${CREATED_THREAD_ID}/messages`
    ) {
      // Keep canonical hydration behind an explicit test gate. Assertions made
      // before release can only be satisfied by the real SSE adapters and
      // optimistic renderer, never by this persisted response.
      await canonicalMessagesGate;
      await route.fulfill({
        json: {
          messages: [
            {
              id: ASSISTANT_MESSAGE_ID,
              thread_id: CREATED_THREAD_ID,
              role: 'assistant',
              content: ANSWER,
              citations: citations.map(
                ({ title, content, ...citation }, index) => ({
                  id: `${index + 1}1010101-0101-4101-8101-010101010101`,
                  ...citation,
                  document_title: title,
                  snippet: content,
                })
              ),
              attachments: [],
              token_count: 40,
              created_at: '2026-09-12T12:01:01Z',
              updated_at: '2026-09-12T12:01:01Z',
            },
            {
              id: USER_MESSAGE_ID,
              thread_id: CREATED_THREAD_ID,
              role: 'user',
              content: 'Synthesize every source',
              citations: [],
              attachments: [],
              token_count: 5,
              created_at: '2026-09-12T12:01:00Z',
              updated_at: '2026-09-12T12:01:00Z',
            },
          ],
          total: 2,
          page: 1,
          limit: 50,
          has_more: false,
        },
      });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: url.pathname } });
  });

  await page.route('**/api/v1/agent/stream', async (route) => {
    streamRequest = route.request();
    const body = [
      `event: rag_context\ndata: ${JSON.stringify({ contexts: citations.slice(0, 2) })}\n\n`,
      `event: rag_context\ndata: ${JSON.stringify({ contexts: citations })}\n\n`,
      `event: token\ndata: ${JSON.stringify({ content: ANSWER })}\n\n`,
      `event: done\ndata: ${JSON.stringify({
        status: 'complete',
        thread_id: CREATED_THREAD_ID,
        assistant_message_id: ASSISTANT_MESSAGE_ID,
        client_message_id: USER_MESSAGE_ID,
      })}\n\n`,
    ].join('');
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body,
    });
  });

  return {
    getStreamRequest: () => streamRequest,
    releaseCanonicalMessages,
  };
}

async function assertGroundedAnswer(page: Page): Promise<void> {
  const answer = page.getByTestId('committed-answer').last();
  await expect(answer).toContainText('Alpha evidence');
  await expect(answer.locator('section[aria-label="Sources"] li')).toHaveCount(
    4
  );
  await expect(
    answer.getByRole('button', { name: 'Source 1: Alpha paper' })
  ).toHaveCount(2);
  await expect(
    answer.getByRole('button', { name: 'Source 4: Delta paper' })
  ).toHaveCount(1);

  const locators = answer.getByTestId('citation-locators').locator('li');
  await expect(locators).toHaveCount(5);
  await expect(locators.nth(2)).toHaveAttribute('data-source-position', '3');
  await expect(locators.nth(2)).toHaveAttribute('data-chunk-id', 'alpha-2');
  await expect(locators.nth(2)).toHaveAttribute('data-chunk-index', '2');
  await expect(locators.nth(4)).toHaveAttribute('data-page-number', '21');
}

test('workspace-wide sidebar and citation provenance survive live SSE and reload', async ({
  page,
}) => {
  const apiFixture = await installApiFixture(page);
  await page.goto('/visual-test/agent-citations?new=1');

  const threadRows = page.getByTestId('workspace-thread-list').locator('li');
  await expect(threadRows).toHaveCount(2);
  await expect(threadRows.nth(0)).toHaveAttribute(
    'data-conversation-id',
    '77777777-7777-4777-8777-777777777777'
  );
  await expect(threadRows.nth(1)).toHaveAttribute(
    'data-conversation-id',
    '88888888-8888-4888-8888-888888888888'
  );

  await page.getByTestId('load-more-threads').click();
  await expect(threadRows).toHaveCount(3);
  await expect(
    page.locator('[data-thread-id="44444444-4444-4444-8444-444444444444"]')
  ).toHaveCount(1);
  await expect(
    page.locator('[data-thread-id="44444444-4444-4444-8444-444444444444"]')
  ).toContainText('Writing thread updated');
  await expect(
    page.locator('[data-thread-id="55555555-5555-4555-8555-555555555555"]')
  ).toHaveAttribute(
    'data-conversation-id',
    '99999999-9999-4999-8999-999999999999'
  );

  await page.getByLabel('Message').fill('Synthesize every source');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('active-thread-id')).toHaveText(
    CREATED_THREAD_ID
  );
  await expect(
    page.locator(`[data-thread-id="${CREATED_THREAD_ID}"]`)
  ).toHaveAttribute('data-conversation-id', CREATED_CONVERSATION_ID);
  await expect.poll(() => apiFixture.getStreamRequest()).not.toBeNull();
  const streamBody = apiFixture.getStreamRequest()!.postDataJSON();
  expect(streamBody.page_context.workspace_id).toBe(WORKSPACE_ID);
  // Canonical message hydration is still blocked, so these citations can only
  // come from the real SSE rag_context adapters and optimistic renderer.
  await assertGroundedAnswer(page);

  apiFixture.releaseCanonicalMessages();
  await page.reload();
  await expect(page.getByTestId('active-thread-id')).toHaveText(
    CREATED_THREAD_ID
  );
  await expect(
    page.locator(`[data-thread-id="${CREATED_THREAD_ID}"]`)
  ).toHaveCount(1);
  await assertGroundedAnswer(page);
});
