import { randomUUID } from 'node:crypto';
import type { components } from '../src/types/generated/api';
import { loadConfig, saveConfig } from './auth/store';
import { getApiBase, getCliAuthHeaders } from './services/client';
import { appendTurn } from './services/messageStore';

export type StreamEvent =
  | { type: 'token'; content: string }
  | { type: 'tool_start'; tool: string; args: string }
  | { type: 'tool_end'; tool: string; isError: boolean; result: string }
  | { type: 'confirmation'; threadId: string; details: Record<string, unknown> }
  | { type: 'plan'; steps: string[]; reasoning: string }
  | {
      type: 'reflection';
      passed: boolean;
      issues: string[];
      round: number;
      revising: boolean;
    }
  | { type: 'rag_context'; contexts: Array<Record<string, unknown>> }
  | {
      type: 'usage';
      inputTokens: number;
      outputTokens: number;
      costUsd: number | null;
    }
  | { type: 'done' }
  | { type: 'error'; message: string };

export interface StreamOptions {
  fetchFn?: typeof fetch;
  /** Receives server-assigned thread IDs without polling persisted config. */
  onThreadId?: (threadId: string) => void;
  signal?: AbortSignal;
  idleTimeoutMs?: number;
  clientMessageId?: string;
  history?: Array<
    Pick<components['schemas']['AgentMessage'], 'role' | 'content'>
  >;
  attachmentIds?: NonNullable<
    components['schemas']['AgentExecuteRequest']['attachment_ids']
  >;
}

const DEFAULT_IDLE_MS = (() => {
  const env = process.env.NOUS_STREAM_IDLE_MS;
  const parsed = env ? Number.parseInt(env, 10) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 90000;
})();

function persistThreadId(threadId: string): void {
  const cfg = loadConfig();
  if (cfg && cfg.thread_id !== threadId) {
    saveConfig({ ...cfg, thread_id: threadId });
  }
}

async function* _parseSseBody(
  body: ReadableStream<Uint8Array>,
  onTrace?: (threadId: string) => void,
  signal?: AbortSignal
): AsyncGenerator<StreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let eventType = '';

  try {
    while (true) {
      let readResult: ReadableStreamReadResult<Uint8Array>;
      try {
        readResult = await reader.read();
        if (signal?.aborted) break;
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') break;
        throw err;
      }
      const { done, value } = readResult;
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        if (line.startsWith('event:')) {
          eventType = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          const raw = line.slice(5).trim();
          try {
            const data = JSON.parse(raw);
            if (eventType === 'token' && data.content) {
              yield { type: 'token', content: data.content };
            } else if (eventType === 'tool_start') {
              yield {
                type: 'tool_start',
                tool: data.tool,
                args:
                  typeof data.args === 'string'
                    ? data.args
                    : data.args == null
                      ? ''
                      : JSON.stringify(data.args),
              };
            } else if (eventType === 'tool_end') {
              yield {
                type: 'tool_end',
                tool: data.tool,
                isError: data.is_error ?? false,
                result:
                  typeof data.result === 'string'
                    ? data.result
                    : data.result == null
                      ? ''
                      : JSON.stringify(data.result),
              };
            } else if (eventType === 'confirmation') {
              yield {
                type: 'confirmation',
                threadId: data.thread_id,
                details: data.confirmation ?? {},
              };
            } else if (eventType === 'plan') {
              yield {
                type: 'plan',
                steps: Array.isArray(data.steps) ? data.steps : [],
                reasoning:
                  typeof data.reasoning === 'string' ? data.reasoning : '',
              };
            } else if (eventType === 'reflection') {
              yield {
                type: 'reflection',
                passed: data.passed ?? true,
                issues: Array.isArray(data.issues) ? data.issues : [],
                round: typeof data.round === 'number' ? data.round : 0,
                revising: data.revising ?? false,
              };
            } else if (eventType === 'rag_context') {
              yield {
                type: 'rag_context',
                contexts: Array.isArray(data.contexts) ? data.contexts : [],
              };
            } else if (eventType === 'usage') {
              yield {
                type: 'usage',
                inputTokens:
                  typeof data.input_tokens === 'number' ? data.input_tokens : 0,
                outputTokens:
                  typeof data.output_tokens === 'number'
                    ? data.output_tokens
                    : 0,
                costUsd:
                  typeof data.cost_usd === 'number' ? data.cost_usd : null,
              };
            } else if (eventType === 'trace' && data.thread_id) {
              onTrace?.(data.thread_id);
            } else if (eventType === 'done') {
              yield { type: 'done' };
              return;
            } else if (eventType === 'error') {
              yield { type: 'error', message: data.error ?? 'Unknown error' };
              return;
            }
          } catch {
            /* skip malformed SSE data */
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

async function* _withIdleTimeout(
  body: ReadableStream<Uint8Array>,
  idleMs: number,
  onTrace?: (threadId: string) => void
): AsyncGenerator<StreamEvent> {
  const controller = new AbortController();
  const inner = _parseSseBody(body, onTrace, controller.signal);
  while (true) {
    const next = inner.next();
    let timer: NodeJS.Timeout | null = null;
    const timeout = new Promise<{ done: true; idle: true }>((resolve) => {
      timer = setTimeout(
        () => resolve({ done: true, idle: true } as const),
        idleMs
      );
    });
    const winner = await Promise.race([next, timeout]);
    if (timer) clearTimeout(timer);
    if ((winner as { idle?: boolean }).idle) {
      controller.abort();
      yield {
        type: 'error',
        message: `IDLE_TIMEOUT:${Math.round(idleMs / 1000)}`,
      };
      return;
    }
    const r = winner as IteratorResult<StreamEvent>;
    if (r.done) return;
    yield r.value;
  }
}

export async function* streamAgent(
  message: string,
  pageContext: Record<string, unknown> = {},
  options: StreamOptions = {}
): AsyncGenerator<StreamEvent> {
  const config = loadConfig();
  if (!config) throw new Error('Not logged in');

  const { fetchFn = fetch, signal } = options;
  const headers = getCliAuthHeaders();
  const cmid = options.clientMessageId ?? randomUUID();
  // Both terminal clients select papers as document IDs. PageContext's schema
  // has no paper_id field, so send the selection through the supported attachment contract.
  const attachmentIds = [
    ...new Set([
      ...(options.attachmentIds ?? []),
      ...(typeof pageContext.paper_id === 'string'
        ? [pageContext.paper_id]
        : []),
    ]),
  ];
  if (attachmentIds.length > 10)
    throw new Error(
      'Attach at most 10 documents, including the selected paper.'
    );

  const res = await fetchFn(`${getApiBase()}/agent/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      messages: [
        ...(options.history ?? []).slice(-49),
        { role: 'user', content: message, client_message_id: cmid },
      ],
      ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
      page_context: pageContext,
      thread_id: config.thread_id ?? undefined,
      model: config.model ?? '',
    }),
    signal,
  });

  if (!res.ok || !res.body) {
    yield { type: 'error', message: `Stream failed: ${res.status}` };
    return;
  }

  const idleMs = options.idleTimeoutMs ?? DEFAULT_IDLE_MS;
  let threadId: string | null = config.thread_id ?? null;
  const onTrace = (id: string) => {
    threadId = id;
    persistThreadId(id);
    options.onThreadId?.(id);
  };
  let assistantBuf = '';
  let sawDone = false;
  for await (const evt of _withIdleTimeout(res.body, idleMs, onTrace)) {
    if (evt.type === 'token') assistantBuf += evt.content;
    if (evt.type === 'done') sawDone = true;
    yield evt;
  }
  if (sawDone && threadId && assistantBuf.length > 0) {
    try {
      appendTurn(threadId, {
        user: { content: message, client_message_id: cmid },
        assistant: { content: assistantBuf, model: config.model ?? undefined },
        ended_at: new Date().toISOString(),
      });
    } catch {
      /* cache write is best-effort */
    }
  }
}

export async function* streamConfirm(
  threadId: string,
  confirmed: boolean,
  options: StreamOptions = {}
): AsyncGenerator<StreamEvent> {
  const config = loadConfig();
  if (!config) throw new Error('Not logged in');

  const { fetchFn = fetch, signal } = options;
  const headers = getCliAuthHeaders();

  const res = await fetchFn(`${getApiBase()}/agent/stream/confirm`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ thread_id: threadId, confirmed }),
    signal,
  });

  if (!res.ok || !res.body) {
    yield { type: 'error', message: `Confirm failed: ${res.status}` };
    return;
  }

  const idleMs = options.idleTimeoutMs ?? DEFAULT_IDLE_MS;
  yield* _withIdleTimeout(res.body, idleMs, (id) => {
    persistThreadId(id);
    options.onThreadId?.(id);
  });
}
