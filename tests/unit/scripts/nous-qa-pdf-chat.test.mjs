import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import {
  assertPdfPersistence, assertPdfQuoteRequest, assertPdfRequest, assertPdfStream, assertSelectedAnswer,
  makeSelectionPdf, observePdfUi, parseNativeSse, registerPdfUiReceipt,
} from '../../e2e/qa/pdf-chat.mjs';
import { FixtureLedger } from '../../e2e/qa/session.mjs';
import { registry } from '../../e2e/qa/scenarios.mjs';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const conversationId = '10000000-0000-4000-8000-000000000002';
const threadId = '10000000-0000-4000-8000-000000000003';
const documentId = '10000000-0000-4000-8000-000000000004';
const runId = '10000000-0000-4000-8000-000000000005';
const foreignId = '20000000-0000-4000-8000-000000000001';
const tokens = makeSelectionPdf('test').tokens;
const prompt = 'Return the signal token in the selected page and cite it.';
const answer = `${tokens[1]} [Doc 1]`;
const expected = { workspaceId, threadId, documentId, tokens, prompt, answer, sourcePosition: 1 };
const requestBody = () => ({
  thread_id: threadId, document_only: true, use_rag: true,
  page_context: { workspace_id: workspaceId, metadata: { workspace_thread_id: threadId } },
  attachment_ids: [documentId], selection_page_range: [1, 1],
});
const events = () => [
  { event: 'status', data: { phase: 'accepted', thread_id: threadId, run_id: runId } },
  { event: 'rag_context', data: { contexts: [{ document_id: documentId, page_number: 2, source_position: 1, content: tokens[1] }] } },
  { event: 'token', data: { content: answer } },
  { event: 'done', data: { thread_id: threadId } },
];
const messages = () => [
  { role: 'user', thread_id: threadId, content: prompt },
  { role: 'assistant', thread_id: threadId, content: answer, citations: [{ document_id: documentId, page_number: 2, source_position: 1 }] },
];
const serialize = (items) => Buffer.from(items.map((item) => `event: ${item.event}\ndata: ${JSON.stringify(item.data)}\n\n`).join(''));

function fakeSession() {
  const session = {
    trustedBaseOrigin: 'https://qa.example.test', trustedApiOrigin: 'https://api.qa.example.test',
    page: new EventEmitter(), ledger: new FixtureLedger('pdf-unit'),
    pendingMutations: new Map(), pendingStreams: new Map(), activeRuns: new Map(),
    registerFixture(kind, id, metadata) { this.ledger.register(kind, id, metadata); },
    registerActiveRun(id, thread) { this.ledger.requireOwned('thread', thread); this.activeRuns.set(id, thread); },
  };
  session.registerFixture('workspace', workspaceId);
  return session;
}
const uiRequest = (path, payload) => ({ method: () => 'POST', url: () => `https://qa.example.test${path}`, postDataJSON: () => payload });
const uiResponse = (request, result, options = {}) => ({
  request: () => request, ok: () => true, status: () => 200,
  json: async () => result, body: async () => options.body ?? serialize(events()),
});
async function flush() { await new Promise((resolve) => setImmediate(resolve)); }

// These are local oracle tests; they do not claim the live browser scenario ran.
test('PDF scenario advertises every real prerequisite and one model budget owner', () => {
  const scenario = registry.find((item) => item.id === 'workflow.pdf-chat-selection-stream-reload');
  assert.deepEqual(scenario.prerequisites, ['auth', 'writes', 'model', 'browser']);
  assert.equal(scenario.mode, 'live-model');
  assert.equal(scenario.callsModel, true);
  assert.equal(scenario.createsFixtures, true);
});

test('generated PDF has independent physical pages and byte-correct xref offsets', () => {
  const { bytes, tokens: facts } = makeSelectionPdf('Unique7');
  const source = bytes.toString('ascii');
  assert.match(source, /^%PDF-1.4/);
  assert.match(source, /\/Count 3 \/Kids \[4 0 R 6 0 R 8 0 R\]/);
  assert.equal(new Set(facts).size, 3);
  for (let index = 0; index < 3; index += 1) {
    const stream = source.match(new RegExp(`${5 + 2 * index} 0 obj\\n<< /Length (\\d+) >>\\nstream\\n([\\s\\S]*?)endstream`));
    assert.ok(stream);
    assert.equal(Buffer.byteLength(stream[2]), Number(stream[1]));
    assert.ok(stream[2].includes(facts[index]));
    assert.ok(facts.filter((_, other) => other !== index).every((fact) => !stream[2].includes(fact)));
  }
  const xref = Number(source.match(/startxref\n(\d+)/)[1]);
  assert.equal(source.slice(xref, xref + 4), 'xref');
  const entries = source.slice(xref).split('\n').slice(3, 12);
  entries.forEach((line, index) => assert.ok(source.slice(Number(line.slice(0, 10))).startsWith(`${index + 1} 0 obj`)));
  assert.ok(bytes.byteLength < 4096);
  assert.throws(() => makeSelectionPdf('unescaped(injection)'), /ASCII/);
});

test('explicit range request oracle rejects automatic ranking, off-by-one ranges, foreign scope, and extra attachments', () => {
  assertPdfRequest(requestBody(), expected);
  for (const mutate of [
    (body) => { body.document_only = false; },
    (body) => { body.use_rag = false; },
    (body) => { body.selection_page_range = [2, 2]; },
    (body) => { delete body.selection_page_range; },
    (body) => { body.page_filter_model = 'cohere'; },
    (body) => { body.page_filter_model = null; },
    (body) => { body.attachment_ids.push(foreignId); },
    (body) => { body.attachment_ids = [foreignId]; },
    (body) => { body.thread_id = foreignId; },
    (body) => { body.page_context.metadata.workspace_thread_id = foreignId; },
    (body) => { body.page_context.workspace_id = foreignId; },
  ]) {
    const body = requestBody(); mutate(body);
    assert.throws(() => assertPdfRequest(body, expected));
  }
});

test('answer oracle requires evidence absent from the question and rejects off-page facts', () => {
  assert.ok(tokens.every((token) => !prompt.includes(token)));
  assertSelectedAnswer(answer, tokens);
  assert.throws(() => assertSelectedAnswer(prompt, tokens), /omitted/);
  for (const incorrect of [`The token is not ${tokens[1]}. [Doc 1]`, `I cannot determine it; ${tokens[1]} is only a guess. [Doc 1]`, `${tokens[1]}EXTRA [Doc 1]`]) assert.throws(() => assertSelectedAnswer(incorrect, tokens), /exactly/);
  assertSelectedAnswer(`**${tokens[1]}** [Doc 1].`, tokens);
  for (const token of [tokens[0], tokens[2]]) assert.throws(() => assertSelectedAnswer(`${answer} ${token}`, tokens), /off-page/);
});

test('quote request oracle requires one exact neutral quote and keeps answer out of the input', () => {
  const quote = 'page';
  const composed = `> ${quote}\n\n${prompt}`;
  const quotedRequest = () => ({ ...requestBody(), messages: [{ role: 'user', content: composed }] });
  const context = { quote, question: prompt, tokens };
  assert.equal(assertPdfQuoteRequest(quotedRequest(), context), composed);
  for (const mutate of [
    (body) => { body.messages[0].content = prompt; },
    (body) => { body.messages[0].content = `> ${quote}\n\n${composed}`; },
    (body) => { body.messages[0].content = composed.replace('> page', '> Physical'); },
    (body) => { body.messages[0].content = `> ${quote}`; },
    (body) => { body.messages[0].content = composed.replace(prompt, 'A different question'); },
    (body) => { body.messages[0].role = 'assistant'; },
    (body) => body.messages.push({ ...body.messages[0] }),
  ]) {
    const body = quotedRequest(); mutate(body);
    assert.throws(() => assertPdfQuoteRequest(body, context), /lost or duplicated/);
  }
  assert.throws(() => assertPdfQuoteRequest(quotedRequest(), { ...context, quote: tokens[1] }), /neutral/);
  assert.throws(() => assertPdfQuoteRequest(quotedRequest(), { ...context, question: `${prompt} ${tokens[1]}` }), /already contained/);
  const saved = messages(); saved[0].content = composed;
  assertPdfPersistence(saved, { ...expected, prompt: composed });
  saved[0].content = prompt;
  assert.throws(() => assertPdfPersistence(saved, { ...expected, prompt: composed }), /exactly once/);
});

test('stream oracle requires terminal success and a cited selected-page context', () => {
  assert.deepEqual(assertPdfStream(events(), expected), { answer, runId, sourcePosition: 1 });
  for (const mutate of [
    (items) => items.pop(),
    (items) => items.push({ event: 'error', data: {} }),
    (items) => items.push({ event: 'confirmation', data: {} }),
    (items) => { items[0].data.thread_id = foreignId; },
    (items) => { items[0].data.run_id = 'unknown'; },
    (items) => { items[1].data.contexts[0].page_number = 1; },
    (items) => { items[1].data.contexts[0].document_id = foreignId; },
    (items) => { items[1].data.contexts[0].content += tokens[0]; },
    (items) => { items[2].data.content = tokens[1]; },
    (items) => { items[2].data.content = `${tokens[1]} [Doc 9]`; },
    (items) => { items[2].data.content = `${answer} [Doc 999]`; },
    (items) => items.push({ event: 'rag_context', data: { contexts: [] } }),
  ]) {
    const items = events(); mutate(items);
    assert.throws(() => assertPdfStream(items, expected));
  }
});

test('persistence oracle rejects lost or moved citation and transcript identity', () => {
  assert.equal(assertPdfPersistence(messages(), expected).content, answer);
  for (const mutate of [
    (items) => { items[1].citations = []; },
    (items) => { items[1].citations[0].page_number = 1; },
    (items) => { items[1].citations[0].document_id = foreignId; },
    (items) => { items[1].citations[0].source_position = 4; },
    (items) => { items[1].thread_id = foreignId; },
    (items) => { items[1].content += tokens[2]; },
    (items) => items.push({ ...items[1] }),
    (items) => items.push({ ...items[1], content: tokens[0], citations: [{ document_id: documentId, page_number: 1, source_position: 1 }] }),
    (items) => items.push({ ...items[0] }),
    (items) => items.shift(),
  ]) {
    const items = messages(); mutate(items);
    assert.throws(() => assertPdfPersistence(items, expected));
  }
});

test('native SSE parsing preserves comments, CRLF, multiline data and rejects malformed/big frames', () => {
  assert.deepEqual(parseNativeSse(Buffer.from(': keepalive\r\n\r\nevent: token\r\ndata: {\r\ndata: "content":"value"}\r\n\r\n')), [{ event: 'token', data: { content: 'value' } }]);
  assert.throws(() => parseNativeSse(Buffer.from('data: {}\n\n')), /event name/);
  assert.throws(() => parseNativeSse(Buffer.from('event: done\ndata: broken\n\n')));
  assert.throws(() => parseNativeSse(Buffer.alloc(2 * 1024 * 1024 + 1)), /byte bound/);
});

test('UI fixture receipts require exact owned ancestry, never titles or prefix matching', () => {
  const session = fakeSession();
  assert.throws(() => registerPdfUiReceipt(session, workspaceId, '/api/v2/threads', { conversation_id: foreignId }, { id: threadId, conversation_id: foreignId }), /unowned/);
  assert.throws(() => registerPdfUiReceipt(session, workspaceId, `/api/v2/workspaces/${workspaceId}/conversations`, { workspace_id: workspaceId }, { id: conversationId, workspace_id: foreignId }), /escaped/);
  registerPdfUiReceipt(session, workspaceId, `/api/v2/workspaces/${workspaceId}/conversations`, { workspace_id: workspaceId }, { id: conversationId, workspace_id: workspaceId });
  registerPdfUiReceipt(session, workspaceId, '/api/v2/threads', { conversation_id: conversationId }, { id: threadId, conversation_id: conversationId });
  assert.equal(session.ledger.requireOwned('thread', threadId).metadata.conversationId, conversationId);
});

// Mutation proof: tests/e2e/qa/pdf-chat.mjs:194 receiptQueue await in onResponse.
// /opt/homebrew/opt/node@24/bin/node --test --test-name-pattern='passive observer registers UI parents' tests/unit/scripts/nous-qa-pdf-chat.test.mjs
// Removing await failed with unowned-thread error; exact bytes restored and test passed.
test('passive observer registers UI parents before stream and retains run until durable proof', async () => {
  const session = fakeSession();
  const observer = observePdfUi(session, workspaceId);
  const conversation = uiRequest(`/api/v2/workspaces/${workspaceId}/conversations`, { workspace_id: workspaceId });
  const thread = uiRequest('/api/v2/threads', { conversation_id: conversationId });
  session.page.emit('request', conversation);
  session.page.emit('response', uiResponse(conversation, { id: conversationId, workspace_id: workspaceId }));
  session.page.emit('request', thread);
  session.page.emit('response', uiResponse(thread, { id: threadId, conversation_id: conversationId }));
  const stream = uiRequest('/api/v1/agent/stream', requestBody());
  session.page.emit('request', stream);
  session.page.emit('response', uiResponse(stream));
  await flush();
  observer.check();
  assert.equal(session.pendingMutations.size, 0);
  assert.equal(observer.state.threadId, threadId);
  assert.equal(session.activeRuns.get(runId), threadId);
  assert.equal(session.pendingStreams.size, 1, 'SSE done is not the durable job-status proof');
  assert.deepEqual([...session.pendingStreams.values()][0].acceptedRunIds, [runId]);
  observer.dispose();
  assert.equal(session.page.listenerCount('request'), 0);
});

test('unfinished browser stream stays pending and unowned UI receipt cannot license a run', async () => {
  const session = fakeSession();
  const observer = observePdfUi(session, workspaceId);
  const stream = uiRequest('/api/v1/agent/stream', requestBody());
  session.page.emit('request', stream);
  assert.equal(session.pendingStreams.size, 1);
  assert.equal(observer.state.events, null);
  const foreign = uiRequest('/api/v2/threads', { conversation_id: foreignId });
  session.page.emit('request', foreign);
  session.page.emit('response', uiResponse(foreign, { id: threadId, conversation_id: foreignId }));
  await flush();
  assert.throws(() => observer.check(), /unowned/);
  assert.equal(session.pendingMutations.size, 1);
  assert.equal(session.activeRuns.size, 0);
  assert.equal(session.ledger.owns('thread', threadId), false);
  observer.dispose();
});


test('passive observer ignores foreign origins even when path and echoed ancestry match', async () => {
  const session = fakeSession();
  const observer = observePdfUi(session, workspaceId);
  const request = uiRequest(`/api/v2/workspaces/${workspaceId}/conversations`, { workspace_id: workspaceId });
  request.url = () => `https://foreign.example.test/api/v2/workspaces/${workspaceId}/conversations`;
  session.page.emit('request', request);
  session.page.emit('response', uiResponse(request, { id: conversationId, workspace_id: workspaceId }));
  const stream = uiRequest('/api/v1/agent/stream', requestBody());
  stream.url = () => 'https://foreign.example.test/api/v1/agent/stream';
  session.page.emit('request', stream);
  session.page.emit('response', uiResponse(stream));
  await flush();
  observer.check();
  assert.equal(session.ledger.list().length, 1);
  assert.equal(session.pendingMutations.size, 0);
  assert.equal(session.pendingStreams.size, 0);
  assert.equal(observer.state.events, null);
  observer.dispose();
});
