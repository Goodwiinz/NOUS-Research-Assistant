/**
 * Round-3 audit regressions in the stream transport: H7 (no dead-stream
 * watchdog), M4 (a trimmed replay is accepted silently) and M15 (a 401 on
 * stream open is reported as the user's fault with no retry).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));

const refreshSession = vi.hoisted(() => vi.fn(async () => ({ data: {} })));
const getSession = vi.hoisted(() =>
  vi.fn(async () => ({ data: { session: { access_token: 'token-1' } } }))
);
vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({ auth: { getSession, refreshSession } }),
}));

import {
  agentChatService,
  STREAM_SILENCE_TIMEOUT_MS,
  StreamStalledError,
} from '../agentChatService';

const request = {
  messages: [{ role: 'user', content: 'hi' }],
  page_context: { type: 'chat' },
};

function readerFrom(chunks: string[]): {
  getReader: () => {
    read: () => Promise<ReadableStreamReadResult<Uint8Array>>;
    cancel: () => Promise<void>;
    releaseLock: () => void;
  };
} {
  const encoder = new TextEncoder();
  const queue = chunks.map((c) => encoder.encode(c));
  let i = 0;
  return {
    getReader: () => ({
      async read() {
        if (i >= queue.length) return { done: true, value: undefined };
        return { done: false, value: queue[i++] };
      },
      async cancel() {},
      releaseLock() {},
    }),
  };
}

/** A body whose first read never settles — a silently dead connection. */
function silentReader(): {
  getReader: () => {
    read: () => Promise<ReadableStreamReadResult<Uint8Array>>;
    cancel: () => Promise<void>;
    releaseLock: () => void;
  };
} {
  return {
    getReader: () => ({
      read: () => new Promise<never>(() => {}),
      async cancel() {},
      releaseLock() {},
    }),
  };
}

const realFetch = global.fetch;

describe('agentChatService stream resilience', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    refreshSession.mockResolvedValue({ data: {} });
    getSession.mockResolvedValue({
      data: { session: { access_token: 'token-1' } },
    });
  });
  afterEach(() => {
    global.fetch = realFetch;
    vi.useRealTimers();
  });

  it('gives up on a stream that goes silent (H7)', async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      body: silentReader(),
    })) as unknown as typeof fetch;

    const pending = agentChatService.streamMessage(request, {});
    const assertion =
      expect(pending).rejects.toBeInstanceOf(StreamStalledError);
    await vi.advanceTimersByTimeAsync(STREAM_SILENCE_TIMEOUT_MS + 1_000);
    await assertion;
  });

  it('reports a trimmed replay as a gap instead of accepting it (M4)', async () => {
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      body: readerFrom([
        'id: 4200\nevent: token\ndata: {"content":"tail"}\n\n',
        'event: done\ndata: {"status":"complete"}\n\n',
      ]),
    })) as unknown as typeof fetch;

    const onReplayGap = vi.fn();
    const result = await agentChatService.resumeStream(
      'thread-A',
      12,
      { onReplayGap },
      undefined
    );

    expect(result.status).toBe('resumed');
    expect(onReplayGap).toHaveBeenCalledWith(4200, 13);
  });

  it('does not report a gap when the replay is contiguous', async () => {
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      body: readerFrom([
        'id: 13\nevent: token\ndata: {"content":"next"}\n\n',
        'event: done\ndata: {"status":"complete"}\n\n',
      ]),
    })) as unknown as typeof fetch;

    const onReplayGap = vi.fn();
    await agentChatService.resumeStream('thread-A', 12, { onReplayGap });

    expect(onReplayGap).not.toHaveBeenCalled();
  });

  it('retries a 401 once with a refreshed session (M15)', async () => {
    getSession
      .mockResolvedValueOnce({
        data: { session: { access_token: 'token-old' } },
      })
      .mockResolvedValueOnce({
        data: { session: { access_token: 'token-new' } },
      });
    const fetchMock = vi.fn(async () => {
      if (fetchMock.mock.calls.length === 1) {
        return {
          ok: false,
          status: 401,
          body: null,
          json: async () => ({}),
          text: async () => '',
        };
      }
      return {
        ok: true,
        status: 200,
        body: readerFrom(['event: done\ndata: {"status":"complete"}\n\n']),
      };
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const onDone = vi.fn();
    const onError = vi.fn();
    const onAuthRefreshAttempt = vi.fn();
    const onAuthRefreshSuccess = vi.fn();
    await agentChatService.streamMessage(request, {
      onDone,
      onError,
      onAuthRefreshAttempt,
      onAuthRefreshSuccess,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(onAuthRefreshAttempt).toHaveBeenCalledTimes(1);
    expect(onAuthRefreshSuccess).toHaveBeenCalledTimes(1);
    expect(
      (fetchMock.mock.calls[0]?.[1]?.headers as Headers).get('Authorization')
    ).toBe('Bearer token-old');
    expect(
      (fetchMock.mock.calls[1]?.[1]?.headers as Headers).get('Authorization')
    ).toBe('Bearer token-new');
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
    expect(onAuthRefreshSuccess.mock.invocationCallOrder[0]).toBeLessThan(
      onDone.mock.invocationCallOrder[0]
    );
  });

  it('reports authentication_required after exactly one failed refresh retry', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 401,
      body: null,
      json: async () => ({}),
      text: async () => '',
    }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const onError = vi.fn();
    const onAuthRefreshAttempt = vi.fn();
    const onAuthRefreshSuccess = vi.fn();
    await agentChatService.streamMessage(request, {
      onError,
      onAuthRefreshAttempt,
      onAuthRefreshSuccess,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(onAuthRefreshAttempt).toHaveBeenCalledTimes(1);
    expect(onAuthRefreshSuccess).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    // The wire-category channel stays undefined: a rejected credential is not
    // an invalid request. The third argument is explicitly client-local.
    expect(onError.mock.calls[0][1]).toBeUndefined();
    expect(onError.mock.calls[0][2]).toBe('authentication_required');
  });

  it('reports a successful refresh lifecycle on streamConfirm too', async () => {
    const fetchMock = vi.fn(async () => {
      if (fetchMock.mock.calls.length === 1) {
        return { ok: false, status: 401, body: null };
      }
      return {
        ok: true,
        status: 200,
        body: readerFrom(['event: done\ndata: {"status":"complete"}\n\n']),
      };
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    const onAuthRefreshAttempt = vi.fn();
    const onAuthRefreshSuccess = vi.fn();

    await agentChatService.streamConfirm(
      { thread_id: 'thread-A', confirmed: true },
      { onAuthRefreshAttempt, onAuthRefreshSuccess }
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(onAuthRefreshAttempt).toHaveBeenCalledTimes(1);
    expect(onAuthRefreshSuccess).toHaveBeenCalledTimes(1);
  });

  it('reports permission_denied for 403 without refreshing', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 403,
      body: null,
      text: async () => '',
    }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const onError = vi.fn();
    await agentChatService.streamMessage(request, { onError });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(refreshSession).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][1]).toBeUndefined();
    expect(onError.mock.calls[0][2]).toBe('permission_denied');
  });

  it('keeps an initial network failure on the existing exception path', async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError('network unavailable');
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const onError = vi.fn();
    await expect(
      agentChatService.streamMessage(request, { onError })
    ).rejects.toThrow('network unavailable');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(refreshSession).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it('finishes as soon as the terminal frame arrives, even if EOF never comes', async () => {
    vi.useFakeTimers();
    const encoder = new TextEncoder();
    let handedTerminal = false;
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          async read() {
            if (handedTerminal) return new Promise<never>(() => {});
            handedTerminal = true;
            return {
              done: false,
              value: encoder.encode(
                'event: done\ndata: {"status":"complete"}\n\n'
              ),
            };
          },
          async cancel() {},
          releaseLock() {},
        }),
      },
    })) as unknown as typeof fetch;

    const onDone = vi.fn();
    const onError = vi.fn();
    // Resolves without waiting on the half-open socket's watchdog.
    await agentChatService.streamMessage(request, { onDone, onError });

    expect(onDone).toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });
});
