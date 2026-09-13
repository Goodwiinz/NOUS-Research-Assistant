/**
 * The SSE `error` frame is flat: `{ error: "<string>", category: "<label>" }`.
 * `error` stays a STRING (wire compat); `category` is the server's claim about
 * the cause, surfaced to `onError` as a second argument.
 *
 * Two rules are pinned here:
 *  - a category the server sends is passed through only if this build knows it
 *    (an unknown label degrades to `undefined`, never leaks to the UI);
 *  - HTTP-level failures on new streams get a client-derived fallback;
 *    resume failures return a retryable result without firing stream callbacks.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: { getSession: async () => ({ data: { session: null } }) },
  }),
}));

import { agentChatService } from '../agentChatService';

function makeReader(chunks: string[]): {
  read(): Promise<ReadableStreamReadResult<Uint8Array>>;
  releaseLock(): void;
} {
  const encoder = new TextEncoder();
  const queue = chunks.map((c) => encoder.encode(c));
  let i = 0;
  return {
    async read() {
      if (i >= queue.length) return { done: true, value: undefined };
      return { done: false, value: queue[i++] };
    },
    releaseLock() {},
  };
}

function fetchWith(chunks: string[]): typeof fetch {
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    body: { getReader: () => makeReader(chunks) },
  })) as unknown as typeof fetch;
}

function fetchFailing(status: number, body = ''): typeof fetch {
  return vi.fn(async () => ({
    ok: false,
    status,
    body: null,
    text: async () => body,
  })) as unknown as typeof fetch;
}

const request = {
  messages: [{ role: 'user', content: 'hi' }],
  page_context: { type: 'chat' },
};

async function errorFromFrame(
  frame: string
): Promise<[string, string | undefined]> {
  global.fetch = fetchWith([frame]);
  const onError = vi.fn();
  await agentChatService.streamMessage(request, { onError });
  expect(onError).toHaveBeenCalledTimes(1);
  return onError.mock.calls[0] as [string, string | undefined];
}

describe('consumeSse error frame category', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('passes a known server category through to onError', async () => {
    const [message, category] = await errorFromFrame(
      'event: error\ndata: {"error":"upstream boom","category":"model_error"}\n\n'
    );
    expect(message).toBe('upstream boom');
    expect(category).toBe('model_error');
  });

  it('accepts every category in the frontend mirror', async () => {
    for (const known of [
      'upstream_timeout',
      'model_error',
      'tool_error',
      'checkpoint_unavailable',
      'rate_limited',
      'cancelled',
      'invalid_request',
      'conflict',
      'internal',
    ]) {
      const [, category] = await errorFromFrame(
        `event: error\ndata: {"error":"x","category":"${known}"}\n\n`
      );
      expect(category).toBe(known);
    }
  });

  it('ignores an unknown category from a newer backend', async () => {
    const [message, category] = await errorFromFrame(
      'event: error\ndata: {"error":"x","category":"quantum_flux"}\n\n'
    );
    expect(message).toBe('x');
    expect(category).toBeUndefined();
  });

  it.each(['authentication_required', 'permission_denied'])(
    'never accepts the local-only %s signal from an SSE frame',
    async (localFailure) => {
      global.fetch = fetchWith([
        `event: error\ndata: {"error":"x","category":"${localFailure}"}\n\n`,
      ]);
      const onError = vi.fn();

      await agentChatService.streamMessage(request, { onError });

      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0][1]).toBeUndefined();
      expect(onError.mock.calls[0][2]).toBeUndefined();
    }
  );

  it('ignores a non-string category', async () => {
    const [, category] = await errorFromFrame(
      'event: error\ndata: {"error":"x","category":42}\n\n'
    );
    expect(category).toBeUndefined();
  });

  it('reports no category for a legacy frame that omits it', async () => {
    const [message, category] = await errorFromFrame(
      'event: error\ndata: {"error":"legacy failure"}\n\n'
    );
    expect(message).toBe('legacy failure');
    expect(category).toBeUndefined();
  });

  it('keeps `error` a string — the sibling key did not make it an object', async () => {
    const [message] = await errorFromFrame(
      'event: error\ndata: {"error":"still a string","category":"internal"}\n\n'
    );
    expect(typeof message).toBe('string');
    expect(message).toBe('still a string');
  });
});

describe('HTTP-level failures synthesize a client-derived category', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('maps 429 to rate_limited on streamMessage', async () => {
    global.fetch = fetchFailing(429);
    const onError = vi.fn();
    await agentChatService.streamMessage(request, { onError });
    expect(onError.mock.calls[0][1]).toBe('rate_limited');
  });

  it('maps other 4xx to invalid_request on streamMessage', async () => {
    global.fetch = fetchFailing(422);
    const onError = vi.fn();
    await agentChatService.streamMessage(request, { onError });
    expect(onError.mock.calls[0][1]).toBe('invalid_request');
  });

  it('reports NO category for 5xx — a transport failure is not a server claim', async () => {
    global.fetch = fetchFailing(503);
    const onError = vi.fn();
    await agentChatService.streamMessage(request, { onError });
    expect(onError.mock.calls[0][1]).toBeUndefined();
  });

  it('applies the same mapping on streamConfirm', async () => {
    global.fetch = fetchFailing(429);
    const onError = vi.fn();
    await agentChatService.streamConfirm(
      { thread_id: 't1', confirmed: true },
      { onError }
    );
    expect(onError.mock.calls[0][1]).toBe('rate_limited');
  });

  it('keeps resume transport failures separate from stream error frames', async () => {
    global.fetch = fetchFailing(404);
    const onError = vi.fn();
    const result = await agentChatService.resumeStream('t1', 0, { onError });
    expect(result).toEqual({
      status: 'failed',
      error: 'Stream resume failed: 404',
    });
    expect(onError).not.toHaveBeenCalled();
  });
});
