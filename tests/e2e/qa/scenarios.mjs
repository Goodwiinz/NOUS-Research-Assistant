import { randomUUID } from 'node:crypto';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_STREAM_EVENT_TYPES = new Set(['status', 'token', 'done', 'error', 'confirmation', 'tool_call', 'tool_result', 'metadata']);

function assertThat(condition, message, evidence = []) {
  if (!condition) {
    const error = new Error(message);
    error.evidence = evidence;
    throw error;
  }
}

/** Join only the public token payloads from a stream for deterministic QA. */
export function extractAnswerText(events = []) {
  return events
    .filter((item) => item?.event === 'token')
    .map((item) => item?.data?.content)
    .filter((content) => typeof content === 'string')
    .join('');
}

function streamDiagnostics(events, phase) {
  const values = Array.isArray(events) ? events : [];
  const eventTypes = values.map((item) => SAFE_STREAM_EVENT_TYPES.has(item?.event) ? item.event : 'other');
  const acceptedRunIds = values
    .filter((item) => item?.event === 'status' && item?.data?.phase === 'accepted')
    .map((item) => item?.data?.run_id)
    .filter((runId) => typeof runId === 'string' && UUID.test(runId));
  return {
    phase: String(phase ?? 'stream').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 64),
    eventCount: Math.min(values.length, 100),
    eventTypes: [...new Set(eventTypes)].slice(0, 20),
    tokenEventCount: Math.min(values.filter((item) => item?.event === 'token').length, 100),
    acceptedRunIds: [...new Set(acceptedRunIds)].slice(0, 8),
  };
}

function normalizedAnswer(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function answerDiagnostics(events, expected, phase) {
  const rawAnswer = extractAnswerText(events);
  return {
    ...streamDiagnostics(events, phase),
    exactMatch: rawAnswer === expected,
    formattingNormalizedMatch: normalizedAnswer(rawAnswer) === normalizedAnswer(expected),
    answerEmpty: rawAnswer.trim().length === 0,
    answerLength: Math.min(rawAnswer.length, 4000),
    trimmedAnswerLength: Math.min(rawAnswer.trim().length, 4000),
    expectedLength: Math.min(String(expected ?? '').length, 4000),
  };
}

function assertStreamDone(stream, phase) {
  const events = Array.isArray(stream?.events) ? stream.events : [];
  const diagnostic = {
    ...streamDiagnostics(events, phase),
    terminal: stream?.terminal === true,
    done: events.some((item) => item?.event === 'done'),
    errorCategory: 'stream',
    errorCode: 'MISSING_DONE',
  };
  assertThat(diagnostic.done, 'Stream did not emit a done event', diagnostic);
  return diagnostic;
}

/** Exact answer oracle: empty, partial, formatting-only, and substring matches must fail. */
export function assertExactAnswer(events, expected, phase = 'answer') {
  const diagnostic = answerDiagnostics(events, expected, phase);
  if (diagnostic.answerEmpty) {
    assertThat(false, 'Model answer was empty', { ...diagnostic, errorCategory: 'answer', errorCode: 'ANSWER_EMPTY' });
  }
  if (!diagnostic.exactMatch) {
    // Keep the strict raw comparison. The normalized boolean is diagnostic
    // context only and never turns a formatting mismatch into a pass.
    assertThat(false, 'Model answer did not exactly equal the expected token', { ...diagnostic, errorCategory: 'answer', errorCode: 'ANSWER_MISMATCH' });
  }
  return extractAnswerText(events);
}

export function isCanonicalEmptyThreadMessageList(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
  const keys = Object.keys(data).sort();
  if (keys.join('|') !== 'has_more|limit|messages|page|total') return false;
  return Array.isArray(data.messages)
    && data.messages.length === 0
    && data.total === 0
    && data.has_more === false
    && data.page === 1
    && Number.isInteger(data.limit)
    && data.limit >= 1;
}

/** Idempotency oracle used by the lifecycle case and its local negative test. */
export function assertIdempotentMessage({ firstId, secondId, messages, clientMessageId, content }) {
  assertThat(firstId === secondId, 'Duplicate client_message_id returned a different message ID');
  const matching = (Array.isArray(messages) ? messages : [])
    .filter((item) => item?.client_message_id === clientMessageId);
  assertThat(matching.length === 1, `Expected exactly one persisted message for client_message_id (found ${matching.length})`);
  assertThat(matching[0]?.content === content, 'Idempotent message content was not persisted exactly');
}

function threadUrl(threadId) {
  return `/chat?thread=${encodeURIComponent(threadId)}`;
}

function messageComposer(page) {
  return page.getByRole('textbox', { name: 'Message', exact: true });
}

async function waitForCondition(check, timeoutMs, message) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await check();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, Math.min(100, Math.max(1, deadline - Date.now()))));
  }
  if (lastError) throw lastError;
  throw new Error(message);
}

function responseId(response, key = 'id') {
  const id = response?.data?.[key] ?? response?.data?.document_id;
  assertThat(typeof id === 'string' && UUID.test(id), `Expected a UUID ${key} in the API response`);
  return id;
}

async function makeThread(session, evidence, title = 'lifecycle', options = {}) {
  // All fixture mutations use the same authenticated browser session that
  // owns the campaign. The API must never be exercised anonymously and then
  // mislabeled as an authenticated workflow pass.
  if (typeof session.login === 'function') await session.login();
  const prefix = evidence.fixturePrefix;
  let workspaceId = options.workspaceId ?? null;
  if (workspaceId) {
    // The chat UI resolves /chat against the account's default workspace. A
    // second workspace would be invisible in the same sidebar, so callers
    // that exercise history/drafts must explicitly reuse an owned workspace.
    session.ledger.requireOwned('workspace', workspaceId);
  } else {
    const workspaceResponse = await session.request('/api/v2/workspaces', {
      target: 'backend',
      method: 'POST',
      json: { name: `${prefix} workspace`, description: 'Synthetic QA fixture' },
    });
    workspaceId = responseId(workspaceResponse);
    session.registerFixture('workspace', workspaceId, { title: `${prefix} workspace` });
  }

  const conversationResponse = await session.request('/api/v2/workspaces/' + encodeURIComponent(workspaceId) + '/conversations', {
    target: 'backend',
    method: 'POST',
    json: { workspace_id: workspaceId, title: `${prefix} ${title}` },
  });
  const conversationId = responseId(conversationResponse);
  session.registerFixture('conversation', conversationId, { title: `${prefix} ${title}`, workspaceId });

  const threadResponse = await session.request('/api/v2/threads', {
    target: 'backend',
    method: 'POST',
    json: { conversation_id: conversationId, title: `${prefix} ${title}` },
  });
  const threadId = responseId(threadResponse);
  session.registerFixture('thread', threadId, { title: `${prefix} ${title}`, conversationId });
  return { workspaceId, conversationId, threadId };
}

async function setDefaultWorkspaceCache(page, workspaceId) {
  return page.evaluate((id) => {
    const keys = ['default-workspace-object', 'default-workspace-cached-at', 'default-workspace-id'];
    const previous = Object.fromEntries(keys.map((key) => [key, localStorage.getItem(key)]));
    localStorage.setItem('default-workspace-object', JSON.stringify({ id }));
    localStorage.setItem('default-workspace-cached-at', String(Date.now()));
    localStorage.setItem('default-workspace-id', id);
    return previous;
  }, workspaceId);
}

async function restoreDefaultWorkspaceCache(page, previous) {
  await page.evaluate((values) => {
    for (const [key, value] of Object.entries(values ?? {})) {
      if (value === null || value === undefined) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    }
  }, previous);
}

async function assertTranscriptContains(page, text, label, timeoutMs = 10_000) {
  const marker = page.locator('[data-role="user"], [data-role="assistant"]').filter({ hasText: text });
  await marker.first().waitFor({ state: 'visible', timeout: timeoutMs });
  assertThat(await marker.count() > 0, `${label} did not render the expected transcript message`);
}

async function waitForVisibleUnique(locator, label, timeoutMs) {
  try {
    await locator.first().waitFor({ state: 'visible', timeout: timeoutMs });
  } catch {
    throw new Error(`${label} is missing or not visible`);
  }
  assertThat(await locator.count() === 1, `${label} is missing or not unique`);
}

function fixtureText(prefix) {
  return `${prefix} Kestrel fixture. The control number is 7314. The absent fact is the color amber.`;
}

export async function smokeLogin(session) {
  await session.goto('/login', { timeoutMs: session.config.timeoutMs });
  const page = session.page;
  // React can render the login shell after DOMContentLoaded. Wait for the
  // actual visible controls within the scenario bound before checking
  // uniqueness; network-idle is neither required nor a reliable UI signal.
  const timeoutMs = session.config.timeoutMs;
  await waitForVisibleUnique(page.locator('#email'), 'Login email field', timeoutMs);
  await waitForVisibleUnique(page.locator('#password'), 'Login password field', timeoutMs);
  await waitForVisibleUnique(page.locator('form button[type="submit"]'), 'Login submit control', timeoutMs);
  return { assertion: 'Accessible login fields are rendered', evidence: ['#email', '#password', 'form button[type="submit"]'] };
}

async function authenticatedPage(session, path = '/chat') {
  await session.login();
  await session.goto(path);
  session.assertTrustedBrowserUrl(session.page.url());
  return session.page;
}

const scenarios = [
  {
    id: 'smoke.login-availability',
    title: 'Login page renders accessible authentication fields',
    suite: 'smoke',
    prerequisites: ['browser'],
    mode: 'live',
    run: smokeLogin,
  },
  {
    id: 'smoke.dashboard-protection',
    title: 'Unauthenticated dashboard navigation is protected',
    suite: 'smoke',
    prerequisites: ['browser', 'anonymous'],
    mode: 'live',
    async run(session) {
      await session.goto('/dashboard');
      const path = new URL(session.page.url()).pathname;
      assertThat(path === '/login' || path.startsWith('/login/'), `Dashboard did not redirect to login (landed at ${path})`);
      return { assertion: 'Unauthenticated dashboard redirects to /login', evidence: [path] };
    },
  },
  {
    id: 'smoke.backend-health-identity',
    title: 'Backend health and readiness respond without inventing commit identity',
    suite: 'smoke',
    prerequisites: ['identity'],
    mode: 'live',
    async run(session, evidence) {
      const health = await session.request('/health', { target: 'backend' });
      assertThat(health.status === 200 && health.data?.status === 'healthy', 'Backend /health did not report healthy');
      const readiness = await session.request('/health/readiness', { target: 'backend' });
      assertThat(readiness.status === 200 && readiness.data?.status === 'ready', 'Backend readiness did not report ready');
      const identity = evidence.identity(health.data);
      if (identity.status === 'blocked') return { status: 'BLOCKED', reason: identity.reason, assertion: 'Expected backend identity is required' };
      return {
        assertion: 'Backend health and readiness are healthy; commit identity is only recorded when explicitly exposed',
        evidence: [{ health: health.data?.status, readiness: readiness.data?.status, identity }],
      };
    },
  },
  {
    id: 'smoke.malformed-unauthenticated-request',
    title: 'Malformed unauthenticated thread request is denied',
    suite: 'smoke',
    prerequisites: [],
    mode: 'live',
    async run(session) {
      try {
        const response = await session.request('/api/v2/threads/not-a-uuid', { target: 'backend', forwardAuth: false });
        assertThat([401, 403, 404, 422].includes(response.status), `Malformed unauthenticated request unexpectedly returned ${response.status}`);
        return { assertion: 'Malformed request is denied', evidence: [{ status: response.status }] };
      } catch (error) {
        if ([401, 403, 404, 422].includes(error.status)) return { assertion: 'Malformed request is denied', evidence: [{ status: error.status }] };
        throw error;
      }
    },
  },
  {
    id: 'workflow.workspace-conversation-thread-lifecycle',
    title: 'Owned workspace, conversation, thread, message, and state lifecycle persists',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'lifecycle');
      const clientMessageId = randomUUID();
      const body = {
        thread_id: fixture.threadId,
        role: 'user',
        content: fixtureText(evidence.fixturePrefix),
        client_message_id: clientMessageId,
      };
      const first = await session.request('/api/v2/messages', {
        target: 'backend',
        method: 'POST',
        json: body,
      });
      const firstId = responseId(first);
      const second = await session.request('/api/v2/messages', {
        target: 'backend',
        method: 'POST',
        json: body,
      });
      const secondId = responseId(second);
      const listed = await session.request(`/api/v2/threads/${fixture.threadId}/messages?limit=20`, { target: 'backend' });
      assertThat(Array.isArray(listed.data?.items) || Array.isArray(listed.data?.messages), 'Message list shape is not the OpenAPI contract');
      const values = listed.data?.messages ?? listed.data?.items ?? [];
      assertIdempotentMessage({ firstId, secondId, messages: values, clientMessageId, content: body.content });
      await session.request(`/api/v2/threads/${fixture.threadId}/archive`, { target: 'backend', method: 'POST' });
      await session.request(`/api/v2/threads/${fixture.threadId}/reopen`, { target: 'backend', method: 'POST' });
      const resolved = await session.request(`/api/v2/threads/${fixture.threadId}/resolve`, { target: 'backend', method: 'POST' });
      assertThat(['resolved', 'closed'].includes(String(resolved.data?.status).toLowerCase()), `Thread did not resolve (status ${resolved.data?.status ?? 'unknown'})`);
      return { assertion: 'Lifecycle and state transitions persist for exact owned IDs', evidence: [{ threadId: fixture.threadId, messageId: firstId }] };
    },
  },
  {
    id: 'workflow.q-and-a-memory',
    title: 'Authenticated Q&A emits a terminal answer and preserves a follow-up thread',
    suite: 'workflow',
    prerequisites: ['auth', 'writes', 'model'],
    createsFixtures: true,
    callsModel: true,
    mode: 'live-model',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'qa');
      evidence.consumeModelTurn();
      const first = await session.streamAgent({
        messages: [{ role: 'user', content: `Answer exactly with NOUS_QA_ACK for ${evidence.fixturePrefix}.` }],
        thread_id: fixture.threadId,
        use_rag: false,
      });
      assertStreamDone(first, 'q-and-a-first');
      const firstAnswer = assertExactAnswer(first.events, 'NOUS_QA_ACK', 'q-and-a-first-answer');
      evidence.consumeModelTurn();
      const second = await session.streamAgent({
        messages: [{ role: 'user', content: 'What exact token did I ask you to use? Reply with that token only.' }],
        thread_id: fixture.threadId,
        use_rag: false,
      });
      assertStreamDone(second, 'q-and-a-followup');
      const secondAnswer = assertExactAnswer(second.events, 'NOUS_QA_ACK', 'q-and-a-followup-answer');
      return {
        assertion: 'Two bounded inline-only turns reached done with exact deterministic answers',
        evidence: [{ firstTerminal: first.terminal, secondTerminal: second.terminal, firstAnswerLength: firstAnswer.length, secondAnswerLength: secondAnswer.length }],
      };
    },
  },
  {
    id: 'workflow.message-pagination-order',
    title: 'Owned message pagination preserves bounded chronological order',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'pagination');
      const contents = ['page-one', 'page-two', 'page-three'].map((label) => `${evidence.fixturePrefix} ${label}`);
      for (const content of contents) {
        await session.request('/api/v2/messages', {
          target: 'backend',
          method: 'POST',
          json: { thread_id: fixture.threadId, role: 'user', content, client_message_id: randomUUID() },
        });
      }
      const page = await session.request(`/api/v2/threads/${fixture.threadId}/messages?limit=2&order=desc`, { target: 'backend' });
      const values = page.data?.messages ?? page.data?.items ?? [];
      assertThat(values.length === 2, `Expected exactly two messages in the bounded page (found ${values.length})`);
      assertThat(values.map((item) => item.content).join('|') === contents.slice(1).reverse().join('|'), 'Newest-first bounded page was not returned in the documented order');
      assertThat(page.data?.has_more === true || page.data?.total >= 3, 'Pagination response omitted continuation metadata');
      return { assertion: 'The most recent bounded page contains the expected messages in order', evidence: [{ returned: values.length, hasMore: page.data?.has_more ?? null }] };
    },
  },
  {
    id: 'workflow.q-and-a-thread-isolation',
    title: 'Inline-only Q&A context stays isolated between two owned threads',
    suite: 'workflow',
    prerequisites: ['auth', 'writes', 'model'],
    createsFixtures: true,
    callsModel: true,
    mode: 'live-model',
    async run(session, evidence) {
      const firstFixture = await makeThread(session, evidence, 'qa-isolation-a');
      const secondFixture = await makeThread(session, evidence, 'qa-isolation-b');
      const suffix = evidence.runId.replace(/[^A-Za-z0-9]/g, '').slice(-10);
      const firstMarker = `NOUS_A_${suffix}`;
      const secondMarker = `NOUS_B_${suffix}`;
      evidence.consumeModelTurn();
      const first = await session.streamAgent({
        messages: [{ role: 'user', content: `Reply exactly with ${firstMarker} and no other text.` }],
        thread_id: firstFixture.threadId,
        use_rag: false,
      });
      assertStreamDone(first, 'isolation-first');
      assertExactAnswer(first.events, firstMarker, 'isolation-first-answer');
      evidence.consumeModelTurn();
      const second = await session.streamAgent({
        messages: [{ role: 'user', content: `Reply exactly with ${secondMarker} and no other text.` }],
        thread_id: secondFixture.threadId,
        use_rag: false,
      });
      assertStreamDone(second, 'isolation-second');
      assertExactAnswer(second.events, secondMarker, 'isolation-second-answer');
      evidence.consumeModelTurn();
      const followup = await session.streamAgent({
        messages: [{ role: 'user', content: 'What exact marker did I ask you to use in this thread? Reply with that marker only.' }],
        thread_id: secondFixture.threadId,
        use_rag: false,
      });
      assertStreamDone(followup, 'isolation-followup');
      assertExactAnswer(followup.events, secondMarker, 'isolation-followup-answer');
      evidence.consumeModelTurn();
      const revisit = await session.streamAgent({
        messages: [{ role: 'user', content: 'What exact marker did I ask you to use in this thread? Reply with that marker only.' }],
        thread_id: firstFixture.threadId,
        use_rag: false,
      });
      assertStreamDone(revisit, 'isolation-revisit');
      assertExactAnswer(revisit.events, firstMarker, 'isolation-revisit-answer');
      return {
        assertion: 'Two owned threads return their own inline-only marker with RAG disabled and no attachments',
        evidence: [{ firstMarkerLength: firstMarker.length, secondMarkerLength: secondMarker.length, rag: false, attachments: 0, revisitedFirstThread: true }],
      };
    },
  },
  {
    id: 'workflow.reload-persistence',
    title: 'Persisted thread messages survive a browser reload',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'reload');
      const content = `${evidence.fixturePrefix} reload marker`;
      await session.request('/api/v2/messages', {
        target: 'backend', method: 'POST', json: { thread_id: fixture.threadId, content, role: 'user', client_message_id: randomUUID() },
      });
      await authenticatedPage(session, threadUrl(fixture.threadId));
      const assertRendered = async (label) => {
        await session.page.waitForFunction((threadId) => new URL(window.location.href).searchParams.get('thread') === threadId, fixture.threadId, { timeout: session.config.timeoutMs });
        await assertTranscriptContains(session.page, content, label, session.config.timeoutMs);
        assertThat(new URL(session.page.url()).searchParams.get('thread') === fixture.threadId, `${label} navigated away from the owned thread`);
      };
      await assertRendered('Initial render');
      await session.page.reload({ waitUntil: 'domcontentloaded', timeout: session.config.timeoutMs });
      await assertRendered('Reload');
      const messages = await session.request(`/api/v2/threads/${fixture.threadId}/messages?limit=20`, { target: 'backend' });
      const values = messages.data?.messages ?? messages.data?.items ?? [];
      assertThat(values.some((item) => item.content === content), 'Reload fixture message was not persisted');
      return { assertion: 'Owned message is rendered before and after reload and remains persisted', evidence: [{ threadId: fixture.threadId }] };
    },
  },
  {
    id: 'workflow.unicode-message',
    title: 'Unicode message content round-trips without corruption',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'unicode');
      const content = `${evidence.fixturePrefix} — 東京 · शोध · 🧭`;
      const response = await session.request('/api/v2/messages', {
        target: 'backend', method: 'POST', json: { thread_id: fixture.threadId, content, client_message_id: randomUUID() },
      });
      assertThat(response.data?.content === content, 'Unicode message did not round-trip exactly');
      return { assertion: 'Unicode content is preserved exactly', evidence: [{ messageId: response.data?.id }] };
    },
  },
  {
    id: 'workflow.history-search-and-draft-isolation',
    title: 'History search and draft/thread controls remain scoped to the current UI',
    suite: 'workflow',
    prerequisites: ['auth', 'writes', 'browser'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const firstFixture = await makeThread(session, evidence, 'history-a');
      const secondFixture = await makeThread(session, evidence, 'history-b', { workspaceId: firstFixture.workspaceId });
      const firstTitle = `${evidence.fixturePrefix} history-a`;
      const secondTitle = `${evidence.fixturePrefix} history-b`;
      const page = await session.login();
      const previousCache = await setDefaultWorkspaceCache(page, firstFixture.workspaceId);
      try {
        await session.goto(threadUrl(firstFixture.threadId));
        const rows = page.locator('button.sb-conv');
        await rows.filter({ hasText: firstTitle }).first().waitFor({ state: 'visible', timeout: session.config.timeoutMs });
        await rows.filter({ hasText: secondTitle }).first().waitFor({ state: 'visible', timeout: session.config.timeoutMs });
        const search = page.getByLabel('Search threads');
        await search.fill('history-a');
        await rows.filter({ hasText: firstTitle }).first().waitFor({ state: 'visible', timeout: session.config.timeoutMs });
        await waitForCondition(
          async () => (await rows.filter({ hasText: secondTitle }).count()) === 0,
          session.config.timeoutMs,
          'History search still displayed the other owned conversation'
        );
        assertThat(await search.inputValue() === 'history-a', 'History search input did not retain the query');
        const draft = `${evidence.fixturePrefix} draft only on history-a`;
        const message = messageComposer(page);
        await message.fill(draft);
        assertThat(await message.inputValue() === draft, 'Draft input did not retain its thread-scoped value');
        await search.fill('');
        await rows.filter({ hasText: secondTitle }).first().waitFor({ state: 'visible', timeout: session.config.timeoutMs });
        await rows.filter({ hasText: secondTitle }).first().click();
        await waitForCondition(
          async () => new URL(page.url()).searchParams.get('thread') === secondFixture.threadId
            && (await messageComposer(page).inputValue()) !== draft,
          session.config.timeoutMs,
          'Sidebar switch to history-b did not restore an independent draft'
        );
        assertThat(await messageComposer(page).inputValue() !== draft, 'Draft from history-a leaked into history-b');
        await rows.filter({ hasText: firstTitle }).first().click();
        await waitForCondition(
          async () => new URL(page.url()).searchParams.get('thread') === firstFixture.threadId
            && (await messageComposer(page).inputValue()) === draft,
          session.config.timeoutMs,
          'Sidebar switch back to history-a did not restore its draft'
        );
        assertThat(await messageComposer(page).inputValue() === draft, 'History-a draft was not restored after returning');
        return { assertion: 'History search and in-app sidebar switching keep drafts isolated across two threads in one owned workspace', evidence: ['Search threads', 'button.sb-conv', 'Message', 'thread A/B switch', 'default workspace cache precondition'] };
      } finally {
        await restoreDefaultWorkspaceCache(page, previousCache).catch(() => {});
      }
    },
  },
  {
    id: 'workflow.export-markdown',
    title: 'Owned thread exports through the production Markdown route',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'export');
      await session.request('/api/v2/messages', {
        target: 'backend', method: 'POST', json: { thread_id: fixture.threadId, content: `${evidence.fixturePrefix} export marker`, client_message_id: randomUUID() },
      });
      const response = await session.request(`/api/v1/export/thread/${fixture.threadId}?format=markdown`, { target: 'backend', method: 'POST' });
      assertThat(response.text.includes(evidence.fixturePrefix), 'Markdown export omitted the owned marker');
      return { assertion: 'Markdown export contains the exact owned fixture marker', evidence: [{ contentType: response.headers.get('content-type') }] };
    },
  },
  {
    id: 'workflow.export-pdf-signature',
    title: 'Owned thread PDF export has a PDF byte signature',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'pdf-export');
      await session.request('/api/v2/messages', {
        target: 'backend', method: 'POST', json: { thread_id: fixture.threadId, content: `${evidence.fixturePrefix} PDF marker`, client_message_id: randomUUID() },
      });
      const response = await session.request(`/api/v1/export/thread/${fixture.threadId}?format=pdf`, { target: 'backend', method: 'POST' });
      const signature = new TextDecoder().decode(response.bytes.slice(0, 5));
      assertThat(signature === '%PDF-', 'PDF export did not return the PDF byte signature');
      return { assertion: 'PDF export starts with %PDF-', evidence: [{ byteSignature: signature }] };
    },
  },
  {
    id: 'workflow.stop-active-run',
    title: 'Stop cancels the exact active run and persists a stopped response',
    suite: 'workflow',
    prerequisites: ['auth', 'writes', 'model', 'browser'],
    createsFixtures: true,
    callsModel: true,
    mode: 'live-model',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'stop');
      const prompt = `Keep this bounded QA response working for ${evidence.fixturePrefix}; stop it after acceptance.`;
      let acceptedRunId = null;
      let partialSeen = false;
      let streamSettled = false;
      let cancelSent = false;
      let provenTerminal = false;
      evidence.consumeModelTurn();
      let streamOutcomePromise;
      try {
        // Attach an outcome handler immediately. A cancelled SSE reader may
        // reject before the scenario reaches its later await; leaving that
        // rejection pending can terminate the CLI as an unhandled rejection.
        streamOutcomePromise = session.streamAgent({
          messages: [{ role: 'user', content: prompt }],
          thread_id: fixture.threadId,
          use_rag: false,
        }, {
          timeoutMs: session.config.timeoutMs,
          onEvent: async (event) => {
            if (event.event === 'status' && event.data?.phase === 'accepted' && typeof event.data.run_id === 'string') {
              acceptedRunId = event.data.run_id;
            }
            if (event.event === 'token' && typeof event.data?.content === 'string' && event.data.content.length > 0) partialSeen = true;
          },
        }).then(
          (value) => { streamSettled = true; return { ok: true, value }; },
          (error) => { streamSettled = true; return { ok: false, error }; },
        );
        await waitForCondition(
          () => Boolean(acceptedRunId),
          session.config.timeoutMs,
          'Agent stream did not expose an accepted run identity'
        );
        assertThat(UUID.test(acceptedRunId), 'Accepted stream run identity was not a UUID');
        await waitForCondition(
          () => partialSeen || streamSettled,
          session.config.timeoutMs,
          'Agent stream produced no observable partial output before Stop'
        );
        assertThat(partialSeen && !streamSettled, 'Agent stream completed before the durable Stop control could be exercised');
        const cancelResponse = await session.request(`/api/v1/agent/stream/cancel/${fixture.threadId}`, {
          target: 'backend',
          method: 'POST',
          json: { expected_run_id: acceptedRunId },
        });
        cancelSent = true;
        assertThat(cancelResponse.status === 204, `Stop endpoint returned ${cancelResponse.status}`);
        const outcome = await streamOutcomePromise;
        if (!outcome.ok && outcome.error?.status !== 408) throw outcome.error;
        const stream = outcome.ok ? outcome.value : { terminal: false, aborted: true };
        const statusResponse = await waitForCondition(async () => {
          try {
            const response = await session.request(`/api/v1/agent/jobs/${acceptedRunId}`, { target: 'backend' });
            return response.data?.status === 'cancelled' ? response : false;
          } catch (error) {
            if (error?.status === 404) return false;
            throw error;
          }
        }, session.config.timeoutMs, 'Cancelled run did not reach a durable terminal status');
        const messages = await session.request(`/api/v2/threads/${fixture.threadId}/messages?limit=50`, { target: 'backend' });
        const values = messages.data?.messages ?? messages.data?.items ?? [];
        const stoppedAssistant = [...values].reverse().find((item) => item.role === 'assistant');
        assertThat(stoppedAssistant?.stopped === true, 'Cancelled run did not persist an assistant message marked stopped');
        const stoppedContent = typeof stoppedAssistant.content === 'string' ? stoppedAssistant.content.trim() : '';
        assertThat(stoppedContent.length > 0, 'Cancelled run persisted no stopped assistant output');
        const resumed = await session.request(`/api/v1/agent/stream/resume/${fixture.threadId}?after=0`, { target: 'backend' });
        assertThat(resumed.status === 204, `Cancelled stream resume was not idle (${resumed.status})`);
        session.markRunTerminal(acceptedRunId);
        provenTerminal = true;
        const page = await authenticatedPage(session, threadUrl(fixture.threadId));
        const assertStoppedRendered = async (label) => {
          await page.waitForFunction((threadId) => new URL(window.location.href).searchParams.get('thread') === threadId, fixture.threadId, { timeout: session.config.timeoutMs });
          await assertTranscriptContains(page, stoppedContent.slice(0, Math.min(120, stoppedContent.length)), label, session.config.timeoutMs);
          await page.locator('[title="You stopped this response; the text above is partial."]').first().waitFor({ state: 'visible', timeout: session.config.timeoutMs });
          const stopControl = page.getByLabel('Stop agent');
          assertThat(await stopControl.count() === 0 || !(await stopControl.isVisible()), `${label} left the Stop control active after durable cancellation`);
        };
        await assertStoppedRendered('Initial render');
        await page.reload({ waitUntil: 'domcontentloaded', timeout: session.config.timeoutMs });
        await assertStoppedRendered('Reload');
        return {
          assertion: 'Exact accepted run reaches durable cancelled status, persists non-empty stopped output, and stays idle after resume/reload',
          evidence: [{ threadId: fixture.threadId, runId: acceptedRunId, status: statusResponse.data?.status, streamTerminal: stream.terminal, stoppedOutputLength: stoppedContent.length }],
        };
      } finally {
        if (acceptedRunId && !cancelSent) {
          try {
            await session.request(`/api/v1/agent/stream/cancel/${fixture.threadId}`, {
              target: 'backend', method: 'POST', json: { expected_run_id: acceptedRunId },
            });
            cancelSent = true;
          } catch {
            // Runner cleanup will retain this fixture tree if cancellation is
            // not proven, preserving recovery evidence instead of deleting it.
          }
        }
        if (streamOutcomePromise && !streamSettled) {
          await Promise.race([
            streamOutcomePromise,
            new Promise((resolve) => setTimeout(resolve, Math.min(250, session.config.timeoutMs))),
          ]);
        }
        if (acceptedRunId && provenTerminal) session.markRunTerminal(acceptedRunId);
      }
    },
  },
  {
    id: 'workflow.document-upload-and-attachment',
    title: 'Supported attachment upload returns an owned document fixture',
    suite: 'workflow',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      await session.login();
      const form = new FormData();
      const filename = `${evidence.fixturePrefix.replace(/[^A-Za-z0-9_-]/g, '_')}.txt`;
      form.append('file', new Blob([fixtureText(evidence.fixturePrefix)], { type: 'text/plain' }), filename);
      form.append('title', `${evidence.fixturePrefix} document`);
      form.append('description', 'Synthetic QA attachment');
      const response = await session.request('/api/v1/files/upload', { target: 'backend', method: 'POST', body: form });
      const documentId = responseId(response, 'document_id');
      session.registerFixture('document', documentId, { filename });
      assertThat(response.data?.filename === filename, 'Upload response did not identify the submitted file');
      return { assertion: 'Supported text attachment is returned with an exact owned document ID', evidence: [{ documentId, filename }] };
    },
  },
  {
    id: 'adversarial.invalid-bounded-inputs',
    title: 'Unauthenticated invalid agent inputs are denied before execution',
    suite: 'adversarial',
    prerequisites: [],
    mode: 'live',
    async run(session) {
      const cases = [
        { messages: [], use_rag: false },
        { messages: [{ role: 'assistant', content: 'assistant only' }], use_rag: false },
        { messages: [{ role: 'user', content: 'x'.repeat(32_001) }], use_rag: false },
      ];
      const statuses = [];
      for (const payload of cases) {
        try {
          const response = await session.request('/api/v1/agent/stream', { target: 'backend', method: 'POST', json: payload, forwardAuth: false });
          statuses.push(response.status);
        } catch (error) {
          statuses.push(error.status ?? null);
        }
      }
      assertThat(statuses.every((status) => [401, 403].includes(status)), `Unauthenticated invalid input did not hit the auth gate: ${statuses.join(',')}`);
      return { assertion: 'Unauthenticated malformed requests are denied by authentication; validation is covered separately', evidence: [{ statuses }] };
    },
  },
  {
    id: 'adversarial.authenticated-bounded-validation',
    title: 'Authenticated invalid agent inputs return exact validation errors',
    suite: 'adversarial',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'validation');
      const cases = [
        { messages: [], thread_id: fixture.threadId, use_rag: false },
        { messages: [{ role: 'assistant', content: 'assistant only' }], thread_id: fixture.threadId, use_rag: false },
        { messages: [{ role: 'user', content: 'x'.repeat(32_001) }], thread_id: fixture.threadId, use_rag: false },
      ];
      const statuses = [];
      const reservedAttempts = [];
      for (const payload of cases) {
        // Reserve the campaign budget before each potentially accepted
        // submission. This is deliberately conservative: a 422 is counted
        // as an attempted model turn because the client cannot prove that a
        // future backend revision will reject it before model dispatch.
        reservedAttempts.push(evidence.reserveModelTurn());
        let response;
        try {
          response = await session.streamAgent(payload);
        } catch (error) {
          statuses.push(error.status ?? null);
          continue;
        }
        statuses.push(response.status);
        const acceptedRunIds = response.acceptedRunIds ?? [];
        if (acceptedRunIds.length > 0) {
          // Stop immediately. Continuing would risk submitting more invalid
          // bodies after the backend has already accepted a model run.
          throw new Error(`Invalid input unexpectedly accepted model runs: ${acceptedRunIds.join(',')}`);
        }
      }
      assertThat(statuses.length === cases.length, `Authenticated invalid input did not produce one bounded response per case: ${statuses.join(',')}`);
      assertThat(statuses.every((status) => status === 422), `Authenticated invalid input did not produce exact 422 validation statuses: ${statuses.join(',')}`);
      return {
        assertion: 'Authenticated empty, assistant-only, and overlong bodies tied to an owned thread are rejected with exact 422 responses',
        evidence: [{ statuses, threadId: fixture.threadId, reservedAttempts, budgetSemantics: 'conservative pre-submission attempt accounting' }],
      };
    },
  },
  {
    id: 'adversarial.stale-thread-access',
    title: 'Stale or guessed thread IDs do not disclose authenticated resources',
    suite: 'adversarial',
    prerequisites: ['auth'],
    mode: 'live',
    async run(session) {
      await session.login();
      const stale = '00000000-0000-4000-8000-000000000000';
      const endpoints = [
        { kind: 'thread-detail', path: `/api/v2/threads/${stale}` },
        { kind: 'thread-message-list', path: `/api/v2/threads/${stale}/messages` },
        { kind: 'agent-message-list', path: `/api/v1/agent/threads/${stale}/messages` },
      ];
      const observations = [];
      for (const endpoint of endpoints) {
        try {
          const response = await session.request(endpoint.path, { target: 'backend' });
          if (response.status === 200) {
            const canonicalEmptyList = endpoint.kind === 'thread-message-list'
              && isCanonicalEmptyThreadMessageList(response.data);
            assertThat(canonicalEmptyList, 'Stale thread returned an unexpected successful response shape', {
              phase: 'stale-thread-access',
              endpoint: endpoint.kind,
              status: response.status,
              responseShape: canonicalEmptyList ? 'empty-message-list' : 'unexpected',
            });
            observations.push({ endpoint: endpoint.kind, status: response.status, responseShape: 'empty-message-list' });
            continue;
          }
          assertThat([401, 403, 404].includes(response.status), 'Stale thread endpoint returned an unexpected denial status', {
            phase: 'stale-thread-access', endpoint: endpoint.kind, status: response.status, responseShape: 'denied',
          });
          observations.push({ endpoint: endpoint.kind, status: response.status, responseShape: 'denied' });
        } catch (error) {
          if (error?.evidence) throw error;
          const status = Number.isInteger(error?.status) ? error.status : null;
          assertThat([401, 403, 404].includes(status), 'Stale thread endpoint failed with an unexpected status', {
            phase: 'stale-thread-access', endpoint: endpoint.kind, status, responseShape: 'denied',
          });
          observations.push({ endpoint: endpoint.kind, status, responseShape: 'denied' });
        }
      }
      return { assertion: 'Stale thread endpoints return denial/not-found statuses or the documented canonical empty message list', evidence: [{ phase: 'stale-thread-access', observations }] };
    },
  },
  {
    id: 'adversarial.unauthenticated-owned-thread-denial',
    title: 'Unauthenticated access to a newly created owned thread is denied',
    suite: 'adversarial',
    prerequisites: ['auth', 'writes'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'unauth-owned');
      const statuses = [];
      for (const path of [`/api/v2/threads/${fixture.threadId}`, `/api/v2/threads/${fixture.threadId}/messages`]) {
        try {
          const response = await session.request(path, { target: 'backend', forwardAuth: false });
          statuses.push(response.status);
        } catch (error) {
          statuses.push(error.status ?? null);
        }
      }
      assertThat(statuses.every((status) => [401, 403].includes(status)), `Unauthenticated owned-thread access was not denied: ${statuses.join(',')}`);
      return { assertion: 'Fresh owned thread IDs are not readable without an auth token', evidence: [{ statuses }] };
    },
  },
  {
    id: 'adversarial.missing-thread-ui',
    title: 'Missing thread navigation is handled without exposing another thread',
    suite: 'adversarial',
    prerequisites: ['auth', 'writes', 'browser'],
    createsFixtures: true,
    mode: 'live',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'missing-recovery');
      const marker = `${evidence.fixturePrefix} missing-thread recovery marker`;
      await session.request('/api/v2/messages', {
        target: 'backend', method: 'POST',
        json: { thread_id: fixture.threadId, role: 'user', content: marker, client_message_id: randomUUID() },
      });
      const persisted = await session.request(`/api/v2/threads/${fixture.threadId}/messages?limit=20`, { target: 'backend' });
      const persistedValues = persisted.data?.messages ?? persisted.data?.items ?? [];
      const expectedTranscript = [{ role: 'user', content: marker }];
      assertThat(persistedValues.length === expectedTranscript.length, `Missing-thread fixture persisted an unexpected number of messages (${persistedValues.length})`);
      assertThat(persistedValues.every((item, index) => item?.role === expectedTranscript[index].role && item?.content === expectedTranscript[index].content), 'Missing-thread fixture transcript did not match its exact expected message');
      const page = await session.login();
      const previousCache = await setDefaultWorkspaceCache(page, fixture.workspaceId);
      try {
        const response = await session.goto('/chat?thread=00000000-0000-4000-8000-000000000000');
        const path = new URL(session.page.url()).pathname;
        assertThat(response?.status() === undefined || response.status() < 500, 'Missing thread produced a server error page');
        assertThat(path.startsWith('/chat'), `Unexpected missing-thread navigation: ${path}`);
        await waitForCondition(
          () => new URL(page.url()).searchParams.get('thread') === fixture.threadId,
          session.config.timeoutMs,
          'Missing thread did not recover to the explicitly owned workspace fixture'
        );
        const transcript = page.locator('[data-role="user"], [data-role="assistant"]');
        await transcript.first().waitFor({ state: 'visible', timeout: session.config.timeoutMs });
        const renderedMessages = (await transcript.allInnerTexts()).map((text) => text.trim());
        assertThat(renderedMessages.length === expectedTranscript.length, `Missing-thread recovery rendered unexpected transcript entries (${renderedMessages.length})`);
        assertThat(renderedMessages.every((text, index) => text === expectedTranscript[index].content), 'Missing-thread recovery rendered an unexpected or reordered transcript message');
        return { assertion: 'Missing thread recovers inside the explicitly owned workspace and renders exactly its owned transcript', evidence: [{ path, recoveredThreadId: fixture.threadId, expectedCount: expectedTranscript.length, renderedCount: renderedMessages.length }] };
      } finally {
        await restoreDefaultWorkspaceCache(page, previousCache).catch(() => {});
      }
    },
  },
  {
    id: 'adversarial.resume-missing-run',
    title: 'Resume for a missing run is bounded and denied or idle',
    suite: 'adversarial',
    prerequisites: ['auth'],
    mode: 'live',
    async run(session) {
      await session.login();
      const stale = '00000000-0000-4000-8000-000000000000';
      try {
        const response = await session.request(`/api/v1/agent/stream/resume/${stale}?after=0`, { target: 'backend' });
        assertThat([204, 401, 403, 404, 422].includes(response.status), `Missing run resume unexpectedly returned ${response.status}`);
        return { assertion: 'Missing run resume is idle or denied', evidence: [{ status: response.status }] };
      } catch (error) {
        assertThat([401, 403, 404, 422].includes(error.status), `Missing run resume returned unexpected ${error.status}`);
        return { assertion: 'Missing run resume is denied', evidence: [{ status: error.status }] };
      }
    },
  },
  {
    id: 'adversarial.unsupported-attachment',
    title: 'Unsupported attachment is rejected without an owned document',
    suite: 'adversarial',
    prerequisites: ['auth', 'writes', 'browser'],
    mode: 'live',
    async run(session, evidence) {
      const page = await authenticatedPage(session, '/chat');
      await page.getByLabel('Attach file', { exact: true }).setInputFiles({
        name: `${evidence.fixturePrefix}.exe`,
        mimeType: 'application/octet-stream',
        buffer: Buffer.from('MZ'),
      });
      const error = page.getByRole('alert');
      await error.waitFor({ state: 'visible', timeout: session.config.timeoutMs });
      return { assertion: 'Unsupported attachment is rejected with a user-facing alert', evidence: ['Attach file', 'role=alert'] };
    },
  },
  {
    id: 'adversarial.mobile-keyboard-controls',
    title: 'Narrow mobile composer remains keyboard operable without accidental send',
    suite: 'adversarial',
    prerequisites: ['auth', 'browser'],
    mode: 'live',
    async run(session) {
      const page = await authenticatedPage(session, '/chat');
      await page.setViewportSize({ width: 390, height: 844 });
      const message = messageComposer(page);
      const before = await message.inputValue();
      await message.fill('line one');
      await message.press('Shift+Enter');
      await message.type('line two');
      const value = await message.inputValue();
      assertThat(value.includes('line one') && value.includes('line two'), 'Composer lost keyboard-entered text at narrow width');
      assertThat(await page.getByLabel('Attach file').count() === 1, 'Attachment control is not accessible on narrow layout');
      return { assertion: 'Mobile composer retains keyboard text and exposes an accessible attachment control', evidence: [{ beforeLength: before.length, valueLength: value.length }] };
    },
  },
  {
    id: 'adversarial.controlled-transport-abort',
    title: 'Controlled transport abort exposes a retryable UI failure',
    suite: 'adversarial',
    prerequisites: ['auth', 'writes', 'browser'],
    createsFixtures: true,
    mode: 'controlled-transport-fault',
    async run(session, evidence) {
      const fixture = await makeThread(session, evidence, 'controlled-transport');
      const page = await authenticatedPage(session, threadUrl(fixture.threadId));
      assertThat(typeof page.route === 'function', 'Browser route interception is unavailable');
      let interceptedThreadId = null;
      await page.route('**/api/v1/agent/stream', async (route) => {
        try {
          const body = JSON.parse(route.request().postData() ?? '{}');
          interceptedThreadId = body.thread_id ?? null;
        } catch {
          interceptedThreadId = null;
        }
        await route.abort('connectionreset');
      });
      const message = messageComposer(page);
      await message.fill(`${evidence.fixturePrefix} controlled transport fault`);
      await message.press('Enter');
      try {
        await page.getByRole('alert').waitFor({ state: 'visible', timeout: session.config.timeoutMs });
      } finally {
        await page.unroute('**/api/v1/agent/stream');
      }
      assertThat(interceptedThreadId === fixture.threadId, 'Controlled transport fault did not target the exact owned thread');
      const messages = await session.request(`/api/v2/threads/${fixture.threadId}/messages?limit=50`, { target: 'backend' });
      const values = messages.data?.messages ?? messages.data?.items ?? [];
      assertThat(values.every((item) => typeof item?.thread_id !== 'string' || item.thread_id === fixture.threadId), 'Controlled fault created a message outside the owned parent thread');
      return { assertion: 'Controlled browser fault is labeled separately, targets the owned thread, and any created chat messages remain under that parent', evidence: ['route.abort(connectionreset)', 'role=alert', { threadId: fixture.threadId, messageCount: values.length }] };
    },
  },
  {
    id: 'adversarial.hitl-synthetic-scope',
    title: 'HITL approval is blocked until a synthetic fixture operation is observed',
    suite: 'adversarial',
    prerequisites: ['auth', 'writes', 'model'],
    mode: 'live-model',
    callsModel: true,
    async run() {
      return {
        status: 'BLOCKED',
        reason: 'No declared synthetic fixture operation was observed; arbitrary model-proposed actions are never auto-approved',
        assertion: 'HITL safety gate',
      };
    },
  },
];

export const registry = Object.freeze(scenarios.map((scenario) => Object.freeze(scenario)));

export function listScenarios(suite = 'all') {
  const suites = suite === 'all' ? new Set(['smoke', 'workflow', 'adversarial']) : new Set([suite]);
  return registry.filter((scenario) => suites.has(scenario.suite));
}
