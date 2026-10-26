import { act, waitFor } from '@testing-library/react';
import { enableMapSet } from 'immer';
import { beforeEach, describe, expect, it, vi } from 'vitest';

enableMapSet();

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listMessages: vi.fn(),
    deleteThread: vi.fn().mockResolvedValue(undefined),
  },
}));

import { workspaceService } from '@/services/workspaceService';
import { useChatStore } from '@/store/chat-store';
import {
  ChatMessage,
  ChatMessageListResponse,
  MessageRole,
} from '@/types/workspace';

const listMessagesMock = workspaceService.listMessages as ReturnType<
  typeof vi.fn
>;

const makeMessage = (
  id: string,
  threadId = 'thread-a',
  createdAt = `2026-07-15T00:00:${id.replace(/\D/g, '').padStart(2, '0')}Z`,
  clientMessageId?: string
): ChatMessage => ({
  id,
  thread_id: threadId,
  content: `message ${id}`,
  role: MessageRole.USER,
  token_count: 0,
  citations: [],
  attachments: [],
  client_message_id: clientMessageId ?? null,
  created_at: createdAt,
  updated_at: createdAt,
});

const response = (
  messages: ChatMessage[],
  hasMore = false
): ChatMessageListResponse => ({
  messages,
  total: messages.length,
  page: 1,
  limit: 50,
  has_more: hasMore,
});

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

// Task 5.5 mutation-verified two guards this suite covers: the stale-response
// rejection (REQUEST-IDENTITY, `aborts and supersedes an older newest-page
// request for the same thread` below) and the terminal-reconciliation
// expectation check (`leaves freshness stale when the expected persisted or
// runtime row is missing` below). See docs/testing/chat-mutation-checks.md
// items 1 and 2 for the exact guard lines, disable/restore commands, and the
// assertion each mutation breaks.
describe('per-thread newest-page coordinator', () => {
  beforeEach(() => {
    act(() => useChatStore.getState().reset());
    listMessagesMock.mockReset();
  });

  it('aborts and supersedes an older newest-page request for the same thread', async () => {
    const older = deferred<ChatMessageListResponse>();
    const newer = deferred<ChatMessageListResponse>();
    const signals: AbortSignal[] = [];
    listMessagesMock.mockImplementation(
      (_threadId: string, options: { signal?: AbortSignal }) => {
        signals.push(options.signal!);
        return signals.length === 1 ? older.promise : newer.promise;
      }
    );

    const initialLoad = useChatStore.getState().loadMessages('thread-a');
    const terminalRefresh = useChatStore.getState().refreshMessages('thread-a');

    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    newer.resolve(response([makeMessage('m2')]));
    await terminalRefresh;
    older.resolve(response([makeMessage('m1')]));
    await initialLoad;

    expect(
      useChatStore.getState().messages['thread-a'].map((message) => message.id)
    ).toEqual(['m2']);
  });

  it('invalidates an in-flight refresh when a new optimistic turn marks the thread stale', async () => {
    const pending = deferred<ChatMessageListResponse>();
    let signal!: AbortSignal;
    useChatStore.setState({
      messages: { 'thread-a': [makeMessage('optimistic')] },
      messagePagination: {
        'thread-a': { hasMore: false, loadingOlder: false, loadedCount: 1 },
      },
    });
    listMessagesMock.mockImplementation(
      (_threadId: string, options: { signal?: AbortSignal }) => {
        signal = options.signal!;
        return pending.promise;
      }
    );

    const refresh = useChatStore.getState().refreshMessages('thread-a');
    useChatStore.getState().markMessagesStale('thread-a');

    expect(signal.aborted).toBe(true);
    expect(useChatStore.getState().messageFreshness['thread-a']).toBe('stale');

    pending.resolve(response([]));
    await refresh;

    expect(useChatStore.getState().messages['thread-a']).toHaveLength(1);
    expect(useChatStore.getState().messages['thread-a'][0].id).toBe(
      'optimistic'
    );
    expect(useChatStore.getState().messageFreshness['thread-a']).toBe('stale');
  });

  it('keeps requests for different threads independent', async () => {
    const requests = new Map<
      string,
      ReturnType<typeof deferred<ChatMessageListResponse>>
    >();
    const signals = new Map<string, AbortSignal>();
    listMessagesMock.mockImplementation(
      (threadId: string, options: { signal?: AbortSignal }) => {
        const request = deferred<ChatMessageListResponse>();
        requests.set(threadId, request);
        signals.set(threadId, options.signal!);
        return request.promise;
      }
    );

    const loadA = useChatStore.getState().loadMessages('thread-a');
    const loadB = useChatStore.getState().loadMessages('thread-b');

    expect(signals.get('thread-a')?.aborted).toBe(false);
    expect(signals.get('thread-b')?.aborted).toBe(false);

    requests
      .get('thread-b')!
      .resolve(response([makeMessage('b1', 'thread-b')]));
    await loadB;
    requests
      .get('thread-a')!
      .resolve(response([makeMessage('a1', 'thread-a')]));
    await loadA;

    expect(useChatStore.getState().messages['thread-a'][0].id).toBe('a1');
    expect(useChatStore.getState().messages['thread-b'][0].id).toBe('b1');
  });

  it('keeps cached rows renderable while a stale thread refreshes', () => {
    useChatStore.setState({
      messages: { 'thread-a': [makeMessage('m1')] },
      messagePagination: {
        'thread-a': { hasMore: false, loadingOlder: false, loadedCount: 1 },
      },
    });
    listMessagesMock.mockReturnValue(new Promise(() => undefined));

    act(() => {
      useChatStore.getState().markMessagesStale('thread-a');
      useChatStore.getState().setCurrentThread('thread-a');
    });

    expect(useChatStore.getState().messages['thread-a'][0].id).toBe('m1');
    expect(useChatStore.getState().messageFreshness['thread-a']).toBe(
      'refreshing'
    );
    expect(useChatStore.getState().isLoadingMessages).toBe(false);
  });

  it('leaves freshness stale when the expected persisted or runtime row is missing', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    listMessagesMock.mockResolvedValue(response([makeMessage('m1')]));
    useChatStore.getState().markMessagesStale('thread-a');

    const found = await useChatStore.getState().refreshMessages('thread-a', {
      persistedId: 'missing-id',
      runtimeId: 'missing-runtime-id',
      diagnostic: {
        terminalReason: 'done',
        localCount: 2,
        completedInBackground: false,
      },
    });

    expect(found).toBe(false);
    expect(useChatStore.getState().messageFreshness['thread-a']).toBe('stale');
    expect(warn).toHaveBeenCalledOnce();
    expect(warn).toHaveBeenCalledWith(
      '[ChatReconciliationInvariant]',
      expect.objectContaining({
        threadId: 'thread-a',
        terminalReason: 'done',
        freshness: 'stale',
        localCount: 2,
        storeCount: 1,
        requestGeneration: 1,
        completedInBackground: false,
      })
    );
    warn.mockRestore();
  });

  it('does not warn when reconciliation is superseded', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const older = deferred<ChatMessageListResponse>();
    const newer = deferred<ChatMessageListResponse>();
    listMessagesMock
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);

    const first = useChatStore.getState().refreshMessages('thread-a', {
      runtimeId: 'runtime-1',
      diagnostic: {
        terminalReason: 'done',
        localCount: 2,
        completedInBackground: false,
      },
    });
    const second = useChatStore.getState().refreshMessages('thread-a');
    newer.resolve(response([makeMessage('m2')]));
    older.resolve(response([makeMessage('m1')]));
    await Promise.all([first, second]);

    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('marks freshness fresh when an expected runtime row arrives', async () => {
    listMessagesMock.mockResolvedValue(
      response([makeMessage('m1', 'thread-a', undefined, 'runtime-1')])
    );
    useChatStore.getState().markMessagesStale('thread-a');

    const found = await useChatStore.getState().refreshMessages('thread-a', {
      runtimeId: 'runtime-1',
    });

    expect(found).toBe(true);
    expect(useChatStore.getState().messageFreshness['thread-a']).toBe('fresh');
  });

  it('replaces the overlapping newest page while preserving loaded older rows', async () => {
    const existing = ['m1', 'm2', 'm3'].map((id) => makeMessage(id));
    useChatStore.setState({
      messages: { 'thread-a': existing },
      messagePagination: {
        'thread-a': { hasMore: true, loadingOlder: false, loadedCount: 3 },
      },
      messageToThread: Object.fromEntries(
        existing.map((message) => [message.id, 'thread-a'])
      ),
    });
    listMessagesMock.mockResolvedValue(
      response([makeMessage('m4'), makeMessage('m3')], true)
    );

    await useChatStore.getState().refreshMessages('thread-a');

    expect(
      useChatStore.getState().messages['thread-a'].map((message) => message.id)
    ).toEqual(['m1', 'm2', 'm3', 'm4']);
    expect(useChatStore.getState().messagePagination['thread-a']).toEqual({
      message_branching: false,
      branch_document_id: null,
      selected_message_id: null,
      selectingBranch: undefined,
      hasMore: true,
      loadingOlder: false,
      loadedCount: 4,
    });
    expect(useChatStore.getState().messageToThread['m4']).toBe('thread-a');
  });

  it('aborts and removes bookkeeping when a thread cache is cleared', async () => {
    const pending = deferred<ChatMessageListResponse>();
    let signal!: AbortSignal;
    listMessagesMock.mockImplementation(
      (_threadId: string, options: { signal?: AbortSignal }) => {
        signal = options.signal!;
        return pending.promise;
      }
    );

    const refresh = useChatStore.getState().refreshMessages('thread-a');
    useChatStore.getState().clearThread('thread-a');

    expect(signal.aborted).toBe(true);
    expect(
      useChatStore.getState().messageFreshness['thread-a']
    ).toBeUndefined();
    pending.resolve(response([]));
    await refresh;
    expect(useChatStore.getState().messages['thread-a']).toBeUndefined();
  });

  it('aborts every newest-page request and clears freshness on reset', async () => {
    const pendingA = deferred<ChatMessageListResponse>();
    const pendingB = deferred<ChatMessageListResponse>();
    const signals: AbortSignal[] = [];
    listMessagesMock.mockImplementation(
      (_threadId: string, options: { signal?: AbortSignal }) => {
        signals.push(options.signal!);
        return signals.length === 1 ? pendingA.promise : pendingB.promise;
      }
    );

    const refreshA = useChatStore.getState().refreshMessages('thread-a');
    const refreshB = useChatStore.getState().refreshMessages('thread-b');
    useChatStore.getState().reset();

    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(useChatStore.getState().messageFreshness).toEqual({});

    pendingA.resolve(response([]));
    pendingB.resolve(response([]));
    await Promise.all([refreshA, refreshB]);
  });

  it('aborts and removes transcript bookkeeping when a thread is deleted', async () => {
    const pending = deferred<ChatMessageListResponse>();
    let signal!: AbortSignal;
    listMessagesMock.mockImplementation(
      (_threadId: string, options: { signal?: AbortSignal }) => {
        signal = options.signal!;
        return pending.promise;
      }
    );
    useChatStore.setState({
      messages: { 'thread-a': [makeMessage('m1')] },
      messagePagination: {
        'thread-a': { hasMore: false, loadingOlder: false, loadedCount: 1 },
      },
      messageFreshness: { 'thread-a': 'stale' },
    });

    const refresh = useChatStore.getState().refreshMessages('thread-a');
    await useChatStore.getState().deleteThread('thread-a');

    expect(signal.aborted).toBe(true);
    expect(useChatStore.getState().messages['thread-a']).toBeUndefined();
    expect(
      useChatStore.getState().messagePagination['thread-a']
    ).toBeUndefined();
    expect(
      useChatStore.getState().messageFreshness['thread-a']
    ).toBeUndefined();
    pending.resolve(response([]));
    await refresh;
  });

  it('aborts an evicted thread refresh and removes its freshness entry', async () => {
    const pending = deferred<ChatMessageListResponse>();
    let evictedSignal!: AbortSignal;
    useChatStore.setState({
      messages: { 'thread-0': [makeMessage('m0', 'thread-0')] },
      messagePagination: {
        'thread-0': { hasMore: false, loadingOlder: false, loadedCount: 1 },
      },
      messageFreshness: { 'thread-0': 'stale' },
    });
    listMessagesMock.mockImplementation(
      (threadId: string, options: { signal?: AbortSignal }) => {
        if (threadId === 'thread-0') {
          evictedSignal = options.signal!;
          return pending.promise;
        }
        return Promise.resolve(
          response([makeMessage(`m${threadId.slice(7)}`, threadId)])
        );
      }
    );

    const evictedRefresh = useChatStore.getState().refreshMessages('thread-0');
    for (let index = 1; index <= 50; index += 1) {
      await useChatStore.getState().loadMessages(`thread-${index}`);
    }

    expect(evictedSignal.aborted).toBe(true);
    expect(useChatStore.getState().messages['thread-0']).toBeUndefined();
    expect(
      useChatStore.getState().messageFreshness['thread-0']
    ).toBeUndefined();
    pending.resolve(response([]));
    await evictedRefresh;
  });

  it('rejects a stale response after rapidly switching through three threads before any of them settle', async () => {
    // setCurrentThread on three different (uncached) threads in quick
    // succession, each kicking off its own per-thread newest-page request,
    // none of which has resolved yet. Only the last-selected thread should
    // end up owning isLoadingMessages/loadingThreadId once things settle —
    // an earlier thread's resolution must not strand or resurrect them.
    const pending = new Map<
      string,
      ReturnType<typeof deferred<ChatMessageListResponse>>
    >();
    listMessagesMock.mockImplementation((threadId: string) => {
      const request = deferred<ChatMessageListResponse>();
      pending.set(threadId, request);
      return request.promise;
    });

    act(() => {
      useChatStore.getState().setCurrentThread('thread-a');
      useChatStore.getState().setCurrentThread('thread-b');
      useChatStore.getState().setCurrentThread('thread-c');
    });

    expect(useChatStore.getState().currentThreadId).toBe('thread-c');
    expect(useChatStore.getState().loadingThreadId).toBe('thread-c');
    expect(useChatStore.getState().isLoadingMessages).toBe(true);

    // The abandoned thread-a request resolves late — it must populate its
    // own thread's cache but never touch the now-current thread-c's loading
    // flags or messages.
    pending.get('thread-a')!.resolve(response([makeMessage('a1', 'thread-a')]));
    await waitFor(() =>
      expect(
        useChatStore.getState().messages['thread-a']?.map((m) => m.id)
      ).toEqual(['a1'])
    );

    expect(useChatStore.getState().currentThreadId).toBe('thread-c');
    expect(useChatStore.getState().loadingThreadId).toBe('thread-c');
    expect(useChatStore.getState().isLoadingMessages).toBe(true);
    expect(
      useChatStore.getState().messages['thread-a']?.map((m) => m.id)
    ).toEqual(['a1']);
    expect(useChatStore.getState().messages['thread-c']).toBeUndefined();

    // thread-c finally resolves — it, and only it, clears the loading flags.
    pending.get('thread-c')!.resolve(response([makeMessage('c1', 'thread-c')]));
    await waitFor(() =>
      expect(
        useChatStore.getState().messages['thread-c']?.map((m) => m.id)
      ).toEqual(['c1'])
    );
    expect(useChatStore.getState().isLoadingMessages).toBe(false);
    expect(useChatStore.getState().loadingThreadId).toBeNull();

    pending.get('thread-b')!.resolve(response([makeMessage('b1', 'thread-b')]));
    await waitFor(() =>
      expect(
        useChatStore.getState().messages['thread-b']?.map((m) => m.id)
      ).toEqual(['b1'])
    );
  });

  it('never evicts the currently displayed transcript when the cache exceeds its cap', async () => {
    useChatStore.setState({
      currentThreadId: 'thread-0',
      messages: { 'thread-0': [makeMessage('m0', 'thread-0')] },
      messagePagination: {
        'thread-0': { hasMore: false, loadingOlder: false, loadedCount: 1 },
      },
      messageFreshness: { 'thread-0': 'fresh' },
    });
    listMessagesMock.mockImplementation((threadId: string) =>
      Promise.resolve(
        response([makeMessage(`m${threadId.slice(7)}`, threadId)])
      )
    );

    for (let index = 1; index <= 50; index += 1) {
      await useChatStore.getState().loadMessages(`thread-${index}`);
    }

    expect(useChatStore.getState().messages['thread-0']?.[0].id).toBe('m0');
    expect(useChatStore.getState().messageFreshness['thread-0']).toBe('fresh');
    expect(Object.keys(useChatStore.getState().messages)).toHaveLength(50);
  });
});
