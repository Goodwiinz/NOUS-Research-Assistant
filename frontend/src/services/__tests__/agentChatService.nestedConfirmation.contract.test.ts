/**
 * Contract test: nested confirmation on the confirm stream.
 *
 * The backend may emit a FURTHER `confirmation` event on the confirm
 * stream (e.g. ingest confirmed → create_note also needs confirming).
 * streamConfirm MUST dispatch it to onConfirmation, otherwise nested
 * confirmations silently dead-end. Pins the dispatch contract.
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

function makeReader(chunks: string[]) {
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

describe('agentChatService.streamConfirm nested-confirmation contract', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  const confirmRequest = {
    thread_id: 't-1',
    confirmed: true,
    approval_id: 'a'.repeat(64),
  };

  it('dispatches a nested confirmation event to onConfirmation and does not call onDone', async () => {
    // Backend returns after emitting the nested confirmation — no done frame.
    global.fetch = fetchWith([
      'event: token\ndata: {"content":"ok, ingested."}\n\n',
      `event: confirmation\ndata: ${JSON.stringify({ thread_id: 't-1', confirmation: { approval_id: 'b'.repeat(64), tools: [{ name: 'create_note' }], message: 'Create note?' } })}\n\n`,
    ]);
    const tokens: string[] = [];
    const onConfirmation = vi.fn();
    const onDone = vi.fn();
    await agentChatService.streamConfirm(confirmRequest, {
      onToken: (c) => tokens.push(c),
      onConfirmation,
      onDone,
    });
    // Resumed tokens flow before the nested confirmation.
    expect(tokens).toEqual(['ok, ingested.']);
    expect(onConfirmation).toHaveBeenCalledTimes(1);
    expect(onConfirmation).toHaveBeenCalledWith(
      't-1',
      expect.objectContaining({ message: 'Create note?' })
    );
    expect(onDone).not.toHaveBeenCalled();
  });
  it.each([undefined, 'not-a-receipt'])(
    'rejects an expired card before displaying it (%s)',
    async (approvalId) => {
      global.fetch = fetchWith([
        `event: confirmation\ndata: ${JSON.stringify({ thread_id: 't-1', confirmation: { approval_id: approvalId } })}\n\n`,
      ]);
      const onConfirmation = vi.fn();
      const onError = vi.fn();
      await agentChatService.streamConfirm(confirmRequest, {
        onConfirmation,
        onError,
      });
      expect(onConfirmation).not.toHaveBeenCalled();
      expect(onError).toHaveBeenCalledExactlyOnceWith(
        'This approval has expired. Please start a new request.',
        'conflict'
      );
    }
  );
});
