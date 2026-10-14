import { randomUUID } from 'node:crypto';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_STREAM_BYTES = 2 * 1024 * 1024;

function requireThat(value, message) {
  if (!value) throw new Error(message);
}

/** A real, uncompressed three-page PDF; no parser or inference is mocked. */
export function makeSelectionPdf(nonce = randomUUID().replaceAll('-', '')) {
  requireThat(/^[A-Za-z0-9]{1,40}$/.test(nonce), 'PDF nonce must be bounded ASCII');
  const tokens = ['FIRST', 'SELECTED', 'THIRD'].map((name) => `NOUS${name}${nonce}`);
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Count 3 /Kids [4 0 R 6 0 R 8 0 R] >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  for (let index = 0; index < 3; index += 1) {
    const contentId = 5 + index * 2;
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentId} 0 R >>`);
    const text = `BT /F1 16 Tf 54 720 Td (Physical page ${index + 1}) Tj 0 -32 Td /F1 11 Tf (The exact signal token on this physical page is:) Tj 0 -24 Td (${tokens[index]}) Tj ET\n`;
    objects.push(`<< /Length ${Buffer.byteLength(text)} >>\nstream\n${text}endstream`);
  }
  let pdf = '%PDF-1.4\n';
  const offsets = [0];
  for (let index = 0; index < objects.length; index += 1) {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${objects[index]}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  pdf += offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return { bytes: Buffer.from(pdf), tokens };
}

/** This scenario explicitly fills First/Last page; text selection only quotes. */
export function assertPdfRequest(payload, { documentId, workspaceId, threadId }) {
  requireThat(UUID.test(String(threadId)), 'PDF submission has no canonical thread UUID');
  requireThat(payload?.thread_id === threadId, 'PDF stream did not target the UI-created thread');
  requireThat(payload?.page_context?.metadata?.workspace_thread_id === threadId, 'PDF stream lost its canonical history identity');
  requireThat(payload?.page_context?.workspace_id === workspaceId, 'PDF stream left the owned workspace');
  requireThat(payload?.document_only === true && payload?.use_rag === true, 'PDF submission lost document-only retrieval');
  requireThat(JSON.stringify(payload?.selection_page_range) === '[1,1]', 'Explicit PDF range was not zero-based physical page 2');
  requireThat(!Object.hasOwn(payload, 'page_filter_model'), 'Explicit PDF range unexpectedly requested automatic page ranking');
  requireThat(JSON.stringify(payload?.attachment_ids) === JSON.stringify([documentId]), 'PDF submission did not contain exactly the selected document');
}

/** The selection fixture quotes one neutral heading word, never its answer. */
export function assertPdfQuoteRequest(payload, { quote, question, tokens }) {
  requireThat(['Physical', 'page', '2'].includes(quote), 'PDF QA quote was not a neutral page-2 heading word');
  const prompt = `> ${quote}\n\n${question}`;
  requireThat(tokens.every((token) => !prompt.includes(token)), 'PDF QA question already contained its answer token');
  requireThat(payload?.messages?.length === 1 && payload.messages[0]?.role === 'user'
    && payload.messages[0]?.content === prompt, 'PDF submission lost or duplicated the selected quote');
  return prompt;
}

export function assertSelectedAnswer(answer, tokens) {
  requireThat(typeof answer === 'string' && answer.includes(tokens[1]), 'PDF answer omitted the selected-page token');
  requireThat(!answer.includes(tokens[0]) && !answer.includes(tokens[2]), 'PDF answer leaked an off-page token');
  // The prompt asks for only the fact plus its citation. Merely mentioning
  // the token in a negation, guess, or fabricated longer token is not success.
  const bare = answer.replace(/\[(?:Doc )?\d+\]/g, '').trim()
    .replace(/[.,;:!?]+$/, '').trim()
    .replace(/^([`*"'])+|([`*"'])+$/g, '').trim();
  requireThat(bare === tokens[1], 'PDF answer was not exactly the selected fact with citation');
}

export function parseNativeSse(bytes) {
  requireThat(bytes.byteLength <= MAX_STREAM_BYTES, 'PDF stream exceeded the QA byte bound');
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes).replaceAll('\r\n', '\n');
  const events = [];
  for (const frame of text.split('\n\n')) {
    let event = '';
    const data = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if (!data.length) continue; // Native keepalive comments carry no event.
    requireThat(Boolean(event), 'PDF stream frame omitted its native event name');
    const payload = JSON.parse(data.join('\n'));
    requireThat(payload && typeof payload === 'object' && !Array.isArray(payload), 'PDF stream frame has an invalid payload');
    events.push({ event, data: payload });
  }
  return events;
}

function citedPositions(answer) {
  return [...answer.matchAll(/\[(?:Doc )?(\d+)\]/g)].map((match) => Number(match[1]));
}

export function assertPdfStream(events, { documentId, threadId, tokens }) {
  requireThat(!events.some((item) => ['error', 'confirmation'].includes(item.event)), 'PDF stream failed or requested approval');
  requireThat(events.some((item) => item.event === 'done'), 'PDF stream ended without done');
  const accepted = events.filter((item) => item.event === 'status' && item.data?.phase === 'accepted');
  requireThat(accepted.length === 1 && UUID.test(String(accepted[0].data.run_id)), 'PDF stream needs one exact accepted run');
  requireThat(accepted[0].data.thread_id === threadId, 'Accepted PDF run belongs to another thread');
  const answer = events.filter((item) => item.event === 'token').map((item) => item.data.content ?? '').join('');
  assertSelectedAnswer(answer, tokens);
  // rag_context is a replacement snapshot; an empty later snapshot clears it.
  const contextEvent = events.filter((item) => item.event === 'rag_context').at(-1);
  const contexts = contextEvent?.data?.contexts;
  requireThat(Array.isArray(contexts) && contexts.length > 0, 'PDF stream emitted no retrieval context');
  requireThat(contexts.every((item) => item.document_id === documentId && item.page_number === 2), 'PDF stream citations escaped physical page 2');
  requireThat(contexts.some((item) => String(item.content ?? '').includes(tokens[1])), 'PDF retrieval omitted the actual selected fact');
  requireThat(contexts.every((item) => !String(item.content ?? '').includes(tokens[0]) && !String(item.content ?? '').includes(tokens[2])), 'PDF retrieval leaked off-page text');
  const positions = citedPositions(answer);
  requireThat(positions.length > 0 && positions.every((position) => contexts.some((item) => item.source_position === position)), 'PDF answer did not cite only emitted physical-page sources');
  return { answer, runId: accepted[0].data.run_id, sourcePosition: positions.find((position) => contexts.some((item) => item.source_position === position)) };
}

export function assertPdfPersistence(messages, { documentId, threadId, tokens, prompt, answer, sourcePosition }) {
  requireThat(Array.isArray(messages), 'PDF history was not a message list');
  requireThat(messages.every((message) => message.thread_id === threadId), 'PDF history returned another thread');
  const users = messages.filter((message) => message.role === 'user');
  requireThat(users.length === 1 && users[0].content === prompt, 'PDF question was not persisted exactly once');
  const assistants = messages.filter((message) => message.role === 'assistant');
  requireThat(assistants.length === 1 && assistants[0].content === answer, 'PDF streamed answer was not persisted exactly once');
  const assistant = assistants[0];
  assertSelectedAnswer(assistant.content, tokens);
  requireThat(Array.isArray(assistant.citations) && assistant.citations.length > 0, 'PDF physical-page citation was not persisted');
  requireThat(assistant.citations.every((citation) => citation.document_id === documentId && citation.page_number === 2), 'Persisted citations escaped physical page 2');
  requireThat(assistant.citations.some((citation) => citation.source_position === sourcePosition), 'Persisted citation lost the emitted source position');
  requireThat(citedPositions(answer).every((position) => assistant.citations.some((citation) => citation.source_position === position)), 'Persisted answer cites an unavailable source position');
  return assistant;
}

/** Register only POST receipts whose exact parent belongs to this campaign. */
export function registerPdfUiReceipt(session, workspaceId, path, payload, result) {
  session.ledger.requireOwned('workspace', workspaceId);
  requireThat(UUID.test(String(result?.id)), 'UI fixture creation returned no UUID');
  if (path === `/api/v2/workspaces/${workspaceId}/conversations`) {
    requireThat(payload?.workspace_id === workspaceId && result.workspace_id === workspaceId, 'UI conversation escaped the owned workspace');
    session.registerFixture('conversation', result.id, { workspaceId });
    return 'conversation';
  }
  requireThat(path === '/api/v2/threads', 'Unexpected PDF UI fixture route');
  const parent = session.ledger.requireOwned('conversation', payload?.conversation_id);
  requireThat(parent.metadata.workspaceId === workspaceId && result.conversation_id === parent.id, 'UI thread escaped the owned conversation');
  session.registerFixture('thread', result.id, { conversationId: parent.id });
  return 'thread';
}

/** Passive Playwright observation only: no routes, fetch replacement or store writes.
 * Pending UI writes/streams use the session's existing conservative cleanup ledger.
 * A response that never finishes retains fixtures; it cannot be labelled settled.
 */
export function observePdfUi(session, workspaceId) {
  const page = session.page;
  const mutations = new Map();
  const streams = new Map();
  const state = { error: null, threadId: null, payload: null, events: null, streamCount: 0 };
  let receiptQueue = Promise.resolve();
  const fail = (error) => { state.error ??= error; };
  const onRequest = (request) => {
    try {
      const url = new URL(request.url());
      if (![session.trustedBaseOrigin, session.trustedApiOrigin].includes(url.origin)) return;
      const path = url.pathname;
      if (request.method() !== 'POST') return;
      if (path.endsWith('/conversations') || path === '/api/v2/threads') {
        const key = randomUUID();
        const payload = request.postDataJSON();
        const pending = { key, method: 'POST', path, startedAt: new Date().toISOString() };
        session.pendingMutations.set(key, pending);
        mutations.set(request, { key, path, payload });
      } else if (path === '/api/v1/agent/stream') {
        state.streamCount += 1;
        const payload = request.postDataJSON();
        const key = randomUUID();
        session.pendingStreams.set(key, { key, method: 'POST', path, threadId: payload?.thread_id, startedAt: new Date().toISOString() });
        streams.set(request, { key, payload });
        state.payload = payload;
        requireThat(state.streamCount === 1, 'PDF UI submitted more than one model request');
      }
    } catch (error) { fail(error); }
  };
  const onResponse = (response) => {
    const request = response.request();
    const mutation = mutations.get(request);
    if (mutation) {
      receiptQueue = receiptQueue.then(async () => {
        requireThat(response.ok(), 'PDF UI fixture creation failed');
        const result = await response.json();
        const kind = registerPdfUiReceipt(session, workspaceId, mutation.path, mutation.payload, result);
        if (kind === 'thread') state.threadId = result.id;
        session.pendingMutations.delete(mutation.key);
      }).catch(fail);
    }
    const stream = streams.get(request);
    if (stream) {
      void (async () => {
        await receiptQueue;
        if (state.error) return;
        session.ledger.requireOwned('thread', stream.payload?.thread_id);
        requireThat(stream.payload.thread_id === state.threadId, 'PDF stream did not use its observed new thread');
        if (response.status() >= 400 && response.status() < 500) session.pendingStreams.delete(stream.key);
        requireThat(response.ok(), 'PDF UI stream was rejected');
        const events = parseNativeSse(await response.body());
        const accepted = events.filter((item) => item.event === 'status' && item.data?.phase === 'accepted');
        for (const item of accepted) {
          requireThat(item.data.thread_id === state.threadId, 'PDF acceptance changed thread identity');
          session.registerActiveRun(item.data.run_id, state.threadId, { internal: true });
        }
        const pending = session.pendingStreams.get(stream.key);
        if (pending) pending.acceptedRunIds = accepted.map((item) => item.data.run_id);
        // Durable status must still be checked by the scenario before marking terminal.
        state.events = events;
      })().catch(fail);
    }
  };
  page.on('request', onRequest);
  page.on('response', onResponse);
  return {
    state,
    check() { if (state.error) throw state.error; },
    dispose() { page.off('request', onRequest); page.off('response', onResponse); },
  };
}
