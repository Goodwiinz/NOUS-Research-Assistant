import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useChatStore } from '@/store/chat-store';
import { workspaceService } from '@/services/workspaceService';
import {
  MessageRole,
  type ChatMessage,
  type ChatMessageListResponse,
} from '@/types/workspace';

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listMessages: vi.fn(),
    selectMessageBranch: vi.fn(),
  },
}));

const message = (
  id: string,
  seconds: number,
  siblings = [id]
): ChatMessage => ({
  id,
  thread_id: 'thread',
  content: id,
  role: MessageRole.USER,
  parent_message_id: null,
  sibling_user_message_ids: siblings,
  created_at: `2026-10-14T00:00:${String(seconds).padStart(2, '0')}Z`,
  updated_at: '2026-10-14T00:00:00Z',
  token_count: 0,
  citations: [],
  attachments: [],
});
const page = (
  head: string,
  messages: ChatMessage[],
  hasMore = false
): ChatMessageListResponse => ({
  messages,
  message_branching: true,
  branch_document_id: 'pdf',
  selected_message_id: head,
  total: messages.length,
  page: 1,
  limit: 50,
  has_more: hasMore,
});
const deferred = <T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: Error) => void;
} => {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
const read = vi.mocked(workspaceService.listMessages);
const select = vi.mocked(workspaceService.selectMessageBranch);
const state = (): ReturnType<typeof useChatStore.getState> =>
  useChatStore.getState();
async function seed(hasMore = false): Promise<void> {
  useChatStore.setState({ currentThreadId: 'thread' });
  read.mockResolvedValueOnce(
    page(
      'old-head',
      [
        message('old-head', 9),
        message('old-root', 1, ['old-root', 'new-root']),
      ],
      hasMore
    )
  );
  await state().refreshMessages('thread');
}

describe('canonical PDF branch cache', () => {
  beforeEach(() => {
    state().reset();
    vi.clearAllMocks();
  });

  it.each([false, true])(
    'preserves canonical ancestry instead of sorting timestamps (equal=%s)',
    async (equal) => {
      useChatStore.setState({ currentThreadId: 'thread' });
      const rows = [
        message('root-z', 0),
        message('answer-z', equal ? 0 : 12),
        message('child-a', equal ? 0 : 1),
        message('head-a', equal ? 0 : 2),
      ];
      read.mockResolvedValueOnce(page('head-a', [...rows].reverse()));
      await state().refreshMessages('thread');
      expect(state().messages.thread.map((row) => row.id)).toEqual(
        rows.map((row) => row.id)
      );
      read.mockResolvedValueOnce(page('head-a', rows.slice(2).reverse()));
      await state().refreshMessages('thread');
      expect(state().messages.thread.map((row) => row.id)).toEqual(
        rows.map((row) => row.id)
      );
    }
  );

  it('drops cached sibling ancestry when a fresh page changes the selected head', async () => {
    await seed(true);
    read.mockResolvedValueOnce(
      page('new-head', [
        message('new-head', 12),
        message('new-root', 10, ['old-root', 'new-root']),
      ])
    );
    await state().refreshMessages('thread');
    expect(state().messages.thread.map((row) => row.id)).toEqual([
      'new-root',
      'new-head',
    ]);
    expect(state().messageToThread['old-root']).toBeUndefined();
    expect(state().messagePagination.thread.selected_message_id).toBe(
      'new-head'
    );
  });

  it('pins older requests to their head and ignores an old page after a new head refresh', async () => {
    await seed(true);
    const old = deferred<ChatMessageListResponse>();
    read.mockReturnValueOnce(old.promise);
    const loading = state().loadOlderMessages('thread');
    expect(read).toHaveBeenLastCalledWith(
      'thread',
      expect.objectContaining({
        before_id: 'old-root',
        selected_message_id: 'old-head',
      })
    );
    read.mockResolvedValueOnce(
      page('new-head', [message('new-head', 12), message('new-root', 10)])
    );
    await state().refreshMessages('thread');
    old.resolve(page('old-head', [message('stale-ancestor', 0)]));
    await loading;
    expect(state().messages.thread.map((row) => row.id)).toEqual([
      'new-root',
      'new-head',
    ]);
  });

  it('rejects a mismatched older-page head even without a competing refresh', async () => {
    await seed(true);
    read.mockResolvedValueOnce(page('other-head', [message('alien-root', 0)]));
    await state().loadOlderMessages('thread');
    expect(state().messages.thread.map((row) => row.id)).toEqual([
      'old-root',
      'old-head',
    ]);
  });

  it('reloads the first page when a pinned cursor is rejected', async () => {
    await seed(true);
    read.mockRejectedValueOnce(new Error('409 selected path changed'));
    read.mockResolvedValueOnce(page('new-head', [message('new-root', 10)]));
    await state().loadOlderMessages('thread');
    expect(state().messages.thread.map((row) => row.id)).toEqual(['new-root']);
    expect(state().messagePagination.thread.selected_message_id).toBe(
      'new-head'
    );
  });

  it('serializes selection, fences earlier reads, and only settles after canonical refresh', async () => {
    await seed(true);
    const stale = deferred<ChatMessageListResponse>();
    read.mockReturnValueOnce(stale.promise);
    const older = state().loadOlderMessages('thread');
    const acknowledged = deferred<void>();
    select.mockReturnValueOnce(acknowledged.promise);
    const switching = state().selectMessageBranch('thread', 'new-root');
    expect(state().messagePagination.thread.selectingBranch).toBe(true);
    expect(await state().selectMessageBranch('thread', 'new-root')).toBe(false);
    expect(select).toHaveBeenCalledTimes(1);
    stale.resolve(page('old-head', [message('stale-root', 0)]));
    await older;
    expect(state().messages.thread.map((row) => row.id)).not.toContain(
      'stale-root'
    );
    const canonical = deferred<ChatMessageListResponse>();
    read.mockReturnValueOnce(canonical.promise);
    acknowledged.resolve();
    await Promise.resolve();
    expect(state().messagePagination.thread.selectingBranch).toBe(true);
    canonical.resolve(page('new-head', [message('new-root', 10)]));
    expect(await switching).toBe(true);
    expect(state().messagePagination.thread.selectingBranch).toBe(false);
    expect(state().messages.thread.map((row) => row.id)).toEqual(['new-root']);
  });

  it('reconciles an uncertain selection response instead of restoring a guessed head', async () => {
    await seed();
    select.mockRejectedValueOnce(new Error('connection lost after commit'));
    read.mockResolvedValueOnce(page('new-head', [message('new-root', 10)]));
    expect(await state().selectMessageBranch('thread', 'new-root')).toBe(false);
    expect(state().messagePagination.thread.selected_message_id).toBe(
      'new-head'
    );
  });

  it('does not let a selection from a reset account refresh or unlock the next account', async () => {
    await seed();
    const old = deferred<void>();
    select.mockReturnValueOnce(old.promise);
    const switching = state().selectMessageBranch('thread', 'new-root');
    state().reset();
    await seed();
    const newSelection = deferred<void>();
    select.mockReturnValueOnce(newSelection.promise);
    const freshSwitch = state().selectMessageBranch('thread', 'new-root');
    const reads = read.mock.calls.length;
    old.resolve();
    expect(await switching).toBe(false);
    expect(read).toHaveBeenCalledTimes(reads);
    expect(state().messagePagination.thread.selectingBranch).toBe(true);
    read.mockResolvedValueOnce(page('new-head', [message('new-root', 10)]));
    newSelection.resolve();
    await freshSwitch;
  });

  it('does not unlock a newer selection after the same thread cache is rebuilt', async () => {
    await seed();
    const old = deferred<void>();
    select.mockReturnValueOnce(old.promise);
    const oldSwitch = state().selectMessageBranch('thread', 'new-root');
    state().clearThread('thread');
    await seed();
    const current = deferred<void>();
    select.mockReturnValueOnce(current.promise);
    const currentSwitch = state().selectMessageBranch('thread', 'new-root');
    const reads = read.mock.calls.length;
    old.resolve();
    expect(await oldSwitch).toBe(false);
    expect(read).toHaveBeenCalledTimes(reads);
    expect(state().messagePagination.thread.selectingBranch).toBe(true);
    read.mockResolvedValueOnce(page('new-head', [message('new-root', 10)]));
    current.resolve();
    await currentSwitch;
  });

  it('refuses arbitrary message IDs, linear chat selection, and active turns', async () => {
    await seed();
    expect(await state().selectMessageBranch('thread', 'unknown')).toBe(false);
    useChatStore.setState({ isStreaming: true });
    expect(await state().selectMessageBranch('thread', 'new-root')).toBe(false);
    expect(select).not.toHaveBeenCalled();
  });
});
