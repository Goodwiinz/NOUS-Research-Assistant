/**
 * Unit tests for agentChatService SSE parsing.
 *
 * Focus: chunk-boundary robustness — an SSE frame split across two
 * reader.read() calls must still parse as one event.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/services/apiClient', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      getSession: async () => ({ data: { session: null } }),
    },
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

const request = {
  messages: [{ role: 'user', content: 'hi' }],
  page_context: { type: 'chat' },
};

describe('agentChatService.streamMessage SSE parsing', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('parses frames that arrive in a single chunk', async () => {
    global.fetch = fetchWith([
      'event: token\ndata: {"content":"hello"}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]);
    const tokens: string[] = [];
    const done = vi.fn();
    await agentChatService.streamMessage(request, {
      onToken: (c) => tokens.push(c),
      onDone: done,
    });
    expect(tokens).toEqual(['hello']);
    expect(done).toHaveBeenCalledTimes(1);
  });

  it('routes summarized reasoning separately from answer tokens', async () => {
    global.fetch = fetchWith([
      'event: reasoning_delta\ndata: {"content":"Checking sources"}\n\n',
      'event: token\ndata: {"content":"The answer"}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]);
    const onReasoningDelta = vi.fn();
    const onToken = vi.fn();

    await agentChatService.streamMessage(request, {
      onReasoningDelta,
      onToken,
    });

    expect(onReasoningDelta).toHaveBeenCalledWith('Checking sources');
    expect(onToken).toHaveBeenCalledWith('The answer');
  });

  it('ignores non-string answer and reasoning deltas', async () => {
    global.fetch = fetchWith([
      'event: token\ndata: {"content":"ok"}\n\n',
      'event: token\ndata: {"content":5}\n\n',
      'event: token\ndata: {"content":"!"}\n\n',
      'event: reasoning_delta\ndata: {"content":"step"}\n\n',
      'event: reasoning_delta\ndata: {"content":{"bad":true}}\n\n',
      'event: reasoning_delta\ndata: {"content":" done"}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]);
    let answer = 'answer:';
    let reasoning = 'reasoning:';

    await agentChatService.streamMessage(request, {
      onToken: (content) => (answer += content),
      onReasoningDelta: (content) => (reasoning += content),
    });

    expect(answer).toBe('answer:ok!');
    expect(reasoning).toBe('reasoning:step done');
  });

  it('parses a frame split between event: and data: lines across chunks', async () => {
    // The event line arrives in chunk 1; the data line arrives in chunk 2.
    // Before Fix 1, eventType would reset to '' between chunks and the event
    // would be dropped silently.
    global.fetch = fetchWith([
      'event: token\n',
      'data: {"content":"split"}\n\n',
    ]);
    const tokens: string[] = [];
    await agentChatService.streamMessage(request, {
      onToken: (c) => tokens.push(c),
    });
    expect(tokens).toEqual(['split']);
  });

  it('tolerates CRLF line endings from intermediate proxies', async () => {
    global.fetch = fetchWith([
      'event: token\r\ndata: {"content":"crlf"}\r\n\r\n',
    ]);
    const tokens: string[] = [];
    await agentChatService.streamMessage(request, {
      onToken: (c) => tokens.push(c),
    });
    expect(tokens).toEqual(['crlf']);
  });

  it('forwards is_error flag to onToolEnd', async () => {
    global.fetch = fetchWith([
      'event: tool_end\ndata: {"tool":"search","result":"err","is_error":true}\n\n',
    ]);
    const onToolEnd = vi.fn();
    await agentChatService.streamMessage(request, { onToolEnd });
    expect(onToolEnd).toHaveBeenCalledWith('search', 'err', true);
  });

  it('defaults is_error to false when backend omits it', async () => {
    global.fetch = fetchWith([
      'event: tool_end\ndata: {"tool":"search","result":"ok"}\n\n',
    ]);
    const onToolEnd = vi.fn();
    await agentChatService.streamMessage(request, { onToolEnd });
    expect(onToolEnd).toHaveBeenCalledWith('search', 'ok', false);
  });

  it('forwards tool invocation ids across matching start/end frames', async () => {
    global.fetch = fetchWith([
      'event: tool_start\ndata: {"tool":"search","args":{"q":"a"},"call_id":"call-a"}\n\n',
      'event: tool_end\ndata: {"tool":"search","result":"ok","call_id":"call-a"}\n\n',
    ]);
    const onToolStart = vi.fn();
    const onToolEnd = vi.fn();
    await agentChatService.streamMessage(request, { onToolStart, onToolEnd });
    expect(onToolStart).toHaveBeenCalledWith('search', { q: 'a' }, 'call-a');
    expect(onToolEnd).toHaveBeenCalledWith('search', 'ok', false, 'call-a');
  });

  it('forwards per-turn token usage to onUsage', async () => {
    global.fetch = fetchWith([
      'event: usage\ndata: {"input_tokens":1234,"output_tokens":340}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]);
    const onUsage = vi.fn();
    await agentChatService.streamMessage(request, { onUsage });
    expect(onUsage).toHaveBeenCalledWith(1234, 340);
  });

  it('forwards heartbeat elapsed_ms as live progress', async () => {
    // The keepalive is the ONLY signal during a silent planner/LLM phase —
    // dropping it (as the consumer used to) leaves a 60s run with no progress.
    global.fetch = fetchWith([
      'event: heartbeat\ndata: {"elapsed_ms":47000}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]);
    const onHeartbeat = vi.fn();
    await agentChatService.streamMessage(request, { onHeartbeat });
    expect(onHeartbeat).toHaveBeenCalledWith(47000);
  });

  it('defaults a heartbeat with no elapsed_ms to zero', async () => {
    global.fetch = fetchWith(['event: heartbeat\ndata: {}\n\n']);
    const onHeartbeat = vi.fn();
    await agentChatService.streamMessage(request, { onHeartbeat });
    expect(onHeartbeat).toHaveBeenCalledWith(0);
  });

  it('forwards phase-aware status updates', async () => {
    global.fetch = fetchWith([
      'event: status\ndata: {"phase":"routing","detail":"Choosing the fastest safe path"}\n\n',
    ]);
    const onStatus = vi.fn();
    await agentChatService.streamMessage(request, { onStatus });
    expect(onStatus).toHaveBeenCalledWith(
      'routing',
      'Choosing the fastest safe path'
    );
  });

  it('forwards the accepted durable run id', async () => {
    global.fetch = fetchWith([
      'event: status\ndata: {"phase":"accepted","run_id":"run-1"}\n\n',
    ]);
    const onRunId = vi.fn();

    await agentChatService.streamMessage(request, { onRunId });

    expect(onRunId).toHaveBeenCalledWith('run-1');
  });

  it('defaults missing token counts to zero on the usage event', async () => {
    global.fetch = fetchWith(['event: usage\ndata: {}\n\n']);
    const onUsage = vi.fn();
    await agentChatService.streamMessage(request, { onUsage });
    expect(onUsage).toHaveBeenCalledWith(0, 0);
  });

  it('skips malformed JSON data lines without aborting the stream', async () => {
    global.fetch = fetchWith([
      'event: token\ndata: {not json}\n\n',
      'event: token\ndata: {"content":"after"}\n\n',
    ]);
    const tokens: string[] = [];
    await agentChatService.streamMessage(request, {
      onToken: (c) => tokens.push(c),
    });
    expect(tokens).toEqual(['after']);
  });

  it('flushes a trailing frame that ended without a final newline', async () => {
    // The server's last chunk ends mid-frame (no trailing \n). Without
    // the defensive buffer flush, the final done event is silently dropped.
    global.fetch = fetchWith([
      'event: token\ndata: {"content":"hello"}\n\n',
      'event: done\ndata: {"status":"complete"}',
    ]);
    const tokens: string[] = [];
    const done = vi.fn();
    await agentChatService.streamMessage(request, {
      onToken: (c) => tokens.push(c),
      onDone: done,
    });
    expect(tokens).toEqual(['hello']);
    expect(done).toHaveBeenCalledTimes(1);
  });

  it('reports EOF before a terminal frame as an error', async () => {
    global.fetch = fetchWith(['event: token\ndata: {"content":"partial"}\n\n']);
    const onError = vi.fn();

    await agentChatService.streamMessage(request, { onError });

    expect(onError).toHaveBeenCalledWith(
      'Stream ended before completion. Please retry.'
    );
  });
});

describe('agentChatService.resumeStream', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('returns idle for 204 without firing callbacks', async () => {
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 204,
      body: null,
    })) as unknown as typeof fetch;
    const onToken = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    const res = await agentChatService.resumeStream('thread-1', 0, {
      onToken,
      onDone,
      onError,
    });
    expect(res).toEqual({ status: 'idle' });
    expect(onToken).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it('returns a failed result for HTTP errors without firing stream callbacks', async () => {
    global.fetch = vi.fn(async () => ({
      ok: false,
      status: 503,
      body: null,
      text: async () => '{"detail":"checkpoint unavailable"}',
    })) as unknown as typeof fetch;
    const onError = vi.fn();

    const res = await agentChatService.resumeStream('thread-1', 0, {
      onError,
    });

    expect(res).toEqual({
      status: 'failed',
      error: 'Stream resume failed (503): checkpoint unavailable',
    });
    expect(onError).not.toHaveBeenCalled();
  });

  it('replays buffered frames and reports increasing seqs via onSeq', async () => {
    global.fetch = fetchWith([
      'id: 3\nevent: token\ndata: {"content":"he"}\n\n',
      'id: 4\nevent: token\ndata: {"content":"llo"}\n\n',
      'id: 5\nevent: done\ndata: {"status":"complete"}\n\n',
    ]);
    const tokens: string[] = [];
    const seqs: number[] = [];
    const onDone = vi.fn();
    const res = await agentChatService.resumeStream('thread-1', 2, {
      onToken: (c) => tokens.push(c),
      onSeq: (s) => seqs.push(s),
      onDone,
    });
    expect(res).toEqual({ status: 'resumed' });
    expect(tokens).toEqual(['he', 'llo']);
    expect(seqs).toEqual([3, 4, 5]);
    expect(onDone).toHaveBeenCalledTimes(1);
    // GET with the after cursor in the query string
    const [url, options] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toContain('/agent/stream/resume/thread-1?after=2');
    expect(new Headers(options?.headers).get('Last-Event-ID')).toBe('2');
  });

  it('fails resume when replay ends without a terminal frame', async () => {
    global.fetch = fetchWith([
      'id: 3\nevent: token\ndata: {"content":"partial"}\n\n',
    ]);

    const res = await agentChatService.resumeStream('thread-1', 2, {});

    expect(res).toEqual({
      status: 'failed',
      error: 'Stream ended before completion. Please retry.',
    });
  });

  it('reports seqs on the live streamMessage path too', async () => {
    global.fetch = fetchWith([
      'id: 1\nevent: token\ndata: {"content":"x"}\n\n',
      'id: 2\nevent: done\ndata: {"status":"complete"}\n\n',
    ]);
    const seqs: number[] = [];
    await agentChatService.streamMessage(request, {
      onSeq: (s) => seqs.push(s),
    });
    expect(seqs).toEqual([1, 2]);
  });
});

describe('agentChatService.cancelPendingConfirmation', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('posts the parked thread to the cancellation endpoint', async () => {
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 204,
    })) as unknown as typeof fetch;

    await agentChatService.cancelPendingConfirmation('thread/with space');

    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/agent/stream/cancel/thread%2Fwith%20space'),
      expect.objectContaining({ method: 'POST' })
    );
  });
});

describe('agentChatService.cancelActiveRun', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('posts the expected active run identity to the cancellation endpoint', async () => {
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 204,
    })) as unknown as typeof fetch;

    await agentChatService.cancelActiveRun('thread/with space', 'run-1');

    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/agent/stream/cancel/thread%2Fwith%20space'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ expected_run_id: 'run-1' }),
      })
    );
  });
});

describe('agentChatService.streamConfirm SSE parsing', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  const confirmRequest = { thread_id: 'thread-1', confirmed: true };

  it('forwards per-turn token usage to onUsage on the confirm path', async () => {
    global.fetch = fetchWith([
      'event: usage\ndata: {"input_tokens":1234,"output_tokens":340}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]);
    const onUsage = vi.fn();
    await agentChatService.streamConfirm(confirmRequest, { onUsage });
    expect(onUsage).toHaveBeenCalledWith(1234, 340);
  });

  it('defaults missing token counts to zero on the confirm usage event', async () => {
    global.fetch = fetchWith(['event: usage\ndata: {}\n\n']);
    const onUsage = vi.fn();
    await agentChatService.streamConfirm(confirmRequest, { onUsage });
    expect(onUsage).toHaveBeenCalledWith(0, 0);
  });
});
