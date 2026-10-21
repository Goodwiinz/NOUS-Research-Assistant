/**
 * PDF chats (message_branching) show a fresh page as the complete canonical
 * branch. The turn's own failure bubble must survive the reconcile that makes
 * the page fresh, while a branch switch drops the previous branch's overlay.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { workspaceService } from '@/services/workspaceService';
import {
  MessageRole,
  type ChatMessage,
  type ChatMessageListResponse,
} from '@/types/workspace';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listWorkspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    createWorkspace: vi.fn(),
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    listMessages: vi.fn(),
    selectMessageBranch: vi.fn(),
    createThread: vi.fn(),
    createMessage: vi.fn(),
  },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

const row = (id: string, seconds: number, siblings = [id]): ChatMessage => ({
  id,
  client_message_id: `runtime-${id}`,
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
const branchPage = (
  head: string,
  messages: ChatMessage[]
): ChatMessageListResponse => ({
  messages,
  message_branching: true,
  branch_document_id: 'pdf',
  selected_message_id: head,
  total: messages.length,
  page: 1,
  limit: 50,
  has_more: false,
});
const read = vi.mocked(workspaceService.listMessages);
const select = vi.mocked(workspaceService.selectMessageBranch);

const streamError = makeChatPageMessage({
  role: 'assistant',
  content: '',
  source: 'local-only',
  runtimeId: 'error-bubble',
  error: {
    message: 'This response failed to generate. Please try again.',
    category: 'stream-error',
  },
});

describe('useChatSession PDF-chat branch overlays', () => {
  beforeEach(() => {
    useChatStore.getState().reset();
    useAuthStore.setState({ isAuthenticated: false } as never);
    vi.clearAllMocks();
  });

  it('keeps the stream-error bubble after the stream-error reconcile, then drops it on a branch switch', async () => {
    const { result } = renderHook(() => useChatSession());
    await act(async () => {
      useChatStore.setState({ currentThreadId: 'thread' });
    });
    // onError: the failed turn's optimistic user row plus its error bubble.
    await act(async () => {
      result.current.setMessages([
        makeChatPageMessage({
          role: 'user',
          content: 'question',
          source: 'optimistic',
          runtimeId: 'runtime-question',
        }),
        streamError,
      ]);
    });
    // reconcileUser('stream-error'): the page refreshes and goes fresh.
    read.mockResolvedValueOnce(
      branchPage('question', [
        row('question', 9),
        row('root', 1, ['root', 'other-root']),
      ])
    );
    await act(async () => {
      await useChatStore.getState().refreshMessages('thread');
    });
    expect(useChatStore.getState().messageFreshness.thread).toBe('fresh');
    expect(
      result.current.displayedMessages.map((message) => message.runtimeId)
    ).toEqual(['runtime-root', 'runtime-question', 'error-bubble']);

    // The user selects the sibling branch: the old branch's bubble goes.
    select.mockResolvedValueOnce(undefined);
    read.mockResolvedValueOnce(
      branchPage('other-root', [row('other-root', 10, ['root', 'other-root'])])
    );
    await act(async () => {
      await useChatStore.getState().selectMessageBranch('thread', 'other-root');
    });
    expect(result.current.messages).toEqual([]);
    expect(
      result.current.displayedMessages.map((message) => message.runtimeId)
    ).toEqual(['runtime-other-root']);
  });
});
