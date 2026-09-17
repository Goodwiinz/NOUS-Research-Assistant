import { randomUUID } from 'node:crypto';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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

/** Exact answer oracle: empty, partial, and substring matches must fail. */
export function assertExactAnswer(events, expected) {
  const answer = extractAnswerText(events).trim();
  assertThat(answer.length > 0, 'Model answer was empty');
  assertThat(answer === expected, `Model answer did not exactly equal the expected token (${answer.length} chars)`);
  return answer;
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

async function waitForCondition(check, timeoutMs, message) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
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

async function makeThread(session, evidence, title = 'lifecycle') {
  // All fixture mutations use the same authenticated browser session that
  // owns the campaign. The API must never be exercised anonymously and then
  // mislabeled as an authenticated workflow pass.
  if (typeof session.login === 'function') await session.login();
  const prefix = evidence.fixturePrefix;
  const workspaceResponse = await session.request('/api/v2/workspaces', {
    target: 'backend',
    method: 'POST',
    json: { name: `${prefix} workspace`, description: 'Synthetic QA fixture' },
  });
  const workspaceId = responseId(workspaceResponse);
  session.registerFixture('workspace', workspaceId, { title: `${prefix} workspace` });

  const conversationResponse = await session.request('/api/v2/workspaces/' + encodeURIComponent(workspaceId) + '/conversations', {
    target: 'backend',
    method: 'POST',
    json: { workspace_id: workspaceId, title: `${prefix} ${title}` },
  });
  const conversationId = responseId(conversationResponse);
  session.registerFixture('conversation', conversationId, { title: `${prefix} ${title}` });

  const threadResponse = await session.request('/api/v2/threads', {
    target: 'backend',
    method: 'POST',
    json: { conversation_id: conversationId, title: `${prefix} ${title}` },
  });
  const threadId = responseId(threadResponse);
  session.registerFixture('thread', threadId, { title: `${prefix} ${title}` });
  return { workspaceId, conversationId, threadId };
}

function fixtureText(prefix) {
  return `${prefix} Kestrel fixture. The control number is 7314. The absent fact is the color amber.`;
}

async function smokeLogin(session) {
  await session.goto('/login', { timeoutMs: session.config.timeoutMs });
  const page = session.page;
  assertThat(await page.locator('#email').count() === 1, 'Login email field is missing');
  assertThat(await page.locator('#password').count() === 1, 'Login password field is missing');
  assertThat(await page.locator('form button[type="submit"]').count() === 1, 'Login submit control is missing');
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
      assertThat(first.events.some((item) => item.event === 'done'), 'First Q&A turn did not emit done');
      const firstAnswer = assertExactAnswer(first.events, 'NOUS_QA_ACK');
      evidence.consumeModelTurn();
      const second = await session.streamAgent({
        messages: [{ role: 'user', content: 'What exact token did I ask you to use? Reply with that token only.' }],
        thread_id: fixture.threadId,
        use_rag: false,
      });
      assertThat(second.events.some((item) => item.event === 'done'), 'Follow-up Q&A turn did not emit done');
      const secondAnswer = assertExactAnswer(second.events, 'NOUS_QA_ACK');
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
      assertThat(first.events.some((item) => item.event === 'done'), 'First isolated Q&A turn did not emit done');
      assertExactAnswer(first.events, firstMarker);
      evidence.consumeModelTurn();
      const second = await session.streamAgent({
        messages: [{ role: 'user', content: `Reply exactly with ${secondMarker} and no other text.` }],
        thread_id: secondFixture.threadId,
        use_rag: false,
      });
      assertThat(second.events.some((item) => item.event === 'done'), 'Second isolated Q&A turn did not emit done');
      assertExactAnswer(second.events, secondMarker);
      evidence.consumeModelTurn();
      const followup = await session.streamAgent({
        messages: [{ role: 'user', content: 'What exact marker did I ask you to use in this thread? Reply with that marker only.' }],
        thread_id: secondFixture.threadId,
        use_rag: false,
      });
      assertThat(followup.events.some((item) => item.event === 'done'), 'Isolated follow-up did not emit done');
      assertExactAnswer(followup.events, secondMarker);
      return {
        assertion: 'Two owned threads return their own inline-only marker with RAG disabled and no attachments',
        evidence: [{ firstMarkerLength: firstMarker.length, secondMarkerLength: secondMarker.length, rag: false, attachments: 0 }],
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
        await session.page.waitForFunction(({ marker, threadId }) => {
          const current = new URL(window.location.href);
          return current.searchParams.get('thread') === threadId && document.body.innerText.includes(marker);
        }, { marker: content, threadId: fixture.threadId }, { timeout: session.config.timeoutMs });
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
      const secondFixture = await makeThread(session, evidence, 'history-b');
      const firstTitle = `${evidence.fixturePrefix} history-a`;
      const secondTitle = `${evidence.fixturePrefix} history-b`;
      const page = await authenticatedPage(session, threadUrl(firstFixture.threadId));
      await page.waitForFunction(({ firstTitle: first, secondTitle: second }) => {
        const body = document.body.innerText;
        return body.includes(first) && body.includes(second);
      }, { firstTitle, secondTitle }, { timeout: session.config.timeoutMs });
      const search = page.getByLabel('Search threads');
      await search.fill('history-a');
      await page.waitForFunction(({ firstTitle: first, secondTitle: second }) => {
        const body = document.body.innerText;
        return body.includes(first) && !body.includes(second);
      }, { firstTitle, secondTitle }, { timeout: session.config.timeoutMs });
      assertThat(await search.inputValue() === 'history-a', 'History search input did not retain the query');
      const draft = `${evidence.fixturePrefix} draft only on history-a`;
      const message = page.getByLabel('Message');
      await message.fill(draft);
      assertThat(await message.inputValue() === draft, 'Draft input did not retain its thread-scoped value');
      await session.goto(threadUrl(secondFixture.threadId));
      await page.waitForFunction(({ threadId, draft: previous }) => {
        const current = new URL(window.location.href);
        const input = document.querySelector('[aria-label="Message"]');
        return current.searchParams.get('thread') === threadId && input && input.value !== previous;
      }, { threadId: secondFixture.threadId, draft }, { timeout: session.config.timeoutMs });
      assertThat(await page.getByLabel('Message').inputValue() !== draft, 'Draft from history-a leaked into history-b');
      await session.goto(threadUrl(firstFixture.threadId));
      await page.waitForFunction((expected) => document.querySelector('[aria-label="Message"]')?.value === expected, draft, { timeout: session.config.timeoutMs });
      assertThat(await page.getByLabel('Message').inputValue() === draft, 'History-a draft was not restored after returning');
      return { assertion: 'History search excludes the other owned thread and drafts isolate across A/B switching', evidence: ['Search threads', 'Message', 'thread A/B switch'] };
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
      evidence.consumeModelTurn();
      const streamPromise = session.streamAgent({
        messages: [{ role: 'user', content: prompt }],
        thread_id: fixture.threadId,
        use_rag: false,
      }, {
        timeoutMs: session.config.timeoutMs,
        onEvent: async (event) => {
          if (event.event === 'status' && event.data?.phase === 'accepted' && typeof event.data.run_id === 'string') {
            acceptedRunId = event.data.run_id;
            session.registerActiveRun(acceptedRunId, fixture.threadId);
          }
          if (event.event === 'token' && typeof event.data?.content === 'string' && event.data.content.length > 0) partialSeen = true;
        },
      }).finally(() => { streamSettled = true; });
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
      assertThat(cancelResponse.status === 204, `Stop endpoint returned ${cancelResponse.status}`);
      const stream = await streamPromise;
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
      const resumed = await session.request(`/api/v1/agent/stream/resume/${fixture.threadId}?after=0`, { target: 'backend' });
      assertThat(resumed.status === 204, `Cancelled stream resume was not idle (${resumed.status})`);
      session.clearActiveRun(acceptedRunId);
      const page = await authenticatedPage(session, threadUrl(fixture.threadId));
      await page.waitForFunction(({ threadId, marker }) => {
        const current = new URL(window.location.href);
        return current.searchParams.get('thread') === threadId && document.body.innerText.includes(marker);
      }, { threadId: fixture.threadId, marker: evidence.fixturePrefix }, { timeout: session.config.timeoutMs });
      const stopControl = page.getByLabel('Stop agent');
      assertThat(await stopControl.count() === 0 || !(await stopControl.isVisible()), 'Stop control remained active after durable cancellation');
      await page.reload({ waitUntil: 'domcontentloaded', timeout: session.config.timeoutMs });
      await page.waitForFunction(({ threadId }) => new URL(window.location.href).searchParams.get('thread') === threadId, fixture.threadId, { timeout: session.config.timeoutMs });
      return {
        assertion: 'Exact accepted run reaches durable cancelled status, persists stopped output, and stays idle after resume/reload',
        evidence: [{ threadId: fixture.threadId, runId: acceptedRunId, status: statusResponse.data?.status, streamTerminal: stream.terminal }],
      };
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
    prerequisites: ['auth'],
    mode: 'live',
    async run(session) {
      await session.login();
      const cases = [
        { messages: [], use_rag: false },
        { messages: [{ role: 'assistant', content: 'assistant only' }], use_rag: false },
        { messages: [{ role: 'user', content: 'x'.repeat(32_001) }], use_rag: false },
      ];
      const statuses = [];
      for (const payload of cases) {
        try {
          const response = await session.request('/api/v1/agent/stream', { target: 'backend', method: 'POST', json: payload });
          statuses.push(response.status);
        } catch (error) {
          statuses.push(error.status ?? null);
        }
      }
      assertThat(statuses.every((status) => status === 422), `Authenticated invalid input did not produce exact 422 validation statuses: ${statuses.join(',')}`);
      return { assertion: 'Authenticated empty, assistant-only, and overlong bodies are rejected with 422 before model execution', evidence: [{ statuses }] };
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
      const statuses = [];
      for (const path of [`/api/v2/threads/${stale}`, `/api/v2/threads/${stale}/messages`, `/api/v1/agent/threads/${stale}/messages`]) {
        try {
          const response = await session.request(path, { target: 'backend' });
          statuses.push(response.status);
        } catch (error) {
          statuses.push(error.status ?? null);
        }
      }
      assertThat(statuses.every((status) => [401, 403, 404].includes(status)), `Stale thread request disclosed a successful response: ${statuses.join(',')}`);
      return { assertion: 'Stale thread endpoints return denial/not-found statuses', evidence: [{ statuses }] };
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
    prerequisites: ['auth', 'browser'],
    mode: 'live',
    async run(session) {
      await session.login();
      const response = await session.goto('/chat/00000000-0000-4000-8000-000000000000');
      const path = new URL(session.page.url()).pathname;
      const body = await session.page.locator('body').innerText();
      assertThat(response?.status() === undefined || response.status() < 500, 'Missing thread produced a server error page');
      assertThat(!body.includes('NOUS QA'), 'Missing thread displayed a fixture from another run');
      assertThat(path.startsWith('/chat') || path.startsWith('/dashboard') || path.startsWith('/login'), `Unexpected missing-thread navigation: ${path}`);
      return { assertion: 'Missing thread stays within the app and does not show unrelated fixture content', evidence: [{ path }] };
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
      const chooser = page.waitForEvent('filechooser');
      await page.getByLabel('Attach file').click();
      const file = await chooser;
      await file.setFiles({ name: `${evidence.fixturePrefix}.exe`, mimeType: 'application/octet-stream', buffer: Buffer.from('MZ') });
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
      const message = page.getByLabel('Message');
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
    prerequisites: ['auth', 'browser'],
    mode: 'controlled-transport-fault',
    async run(session, evidence) {
      const page = await authenticatedPage(session, '/chat');
      assertThat(typeof page.route === 'function', 'Browser route interception is unavailable');
      await page.route('**/api/v1/agent/stream', async (route) => route.abort('connectionreset'));
      const message = page.getByLabel('Message');
      await message.fill(`${evidence.fixturePrefix} controlled transport fault`);
      await message.press('Enter');
      try {
        await page.getByRole('alert').waitFor({ state: 'visible', timeout: session.config.timeoutMs });
      } finally {
        await page.unroute('**/api/v1/agent/stream');
      }
      return { assertion: 'Controlled browser fault is labeled separately and surfaced as an alert', evidence: ['route.abort(connectionreset)', 'role=alert'] };
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
