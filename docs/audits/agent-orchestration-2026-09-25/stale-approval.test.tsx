import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { useChatStore } from '@/store/chat-store';

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const streamMessageMock = vi.fn();
const streamConfirmMock = vi.fn().mockResolvedValue(undefined);
const cancelPendingConfirmationMock = vi.fn().mockResolvedValue(undefined);
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...a: unknown[]) => streamMessageMock(...a),
    streamConfirm: (...a: unknown[]) => streamConfirmMock(...a),
    cancelPendingConfirmation: (...a: unknown[]) =>
      cancelPendingConfirmationMock(...a),
    // The hook probes for a parked HITL confirmation on thread activation;
    // nothing is parked in these scenarios.
    resumeStream: vi.fn().mockResolvedValue({ status: 'idle' }),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn(),
  },
}));

type Cb = {
  onConfirmation: (t: string, c: Record<string, unknown>) => void;
  onDone: (p?: unknown) => void;
  onError: (error: string, category?: string) => void;
};

function makeParams(setMessages: (m: unknown) => void) {
  useChatStore.setState({ currentThreadId: 'thread-A' });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages,
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: 'thread-A',
    setActiveConversationId: vi.fn(),
    activeConversationIdRef: { current: 'thread-A' as string | null },
    dbConversation: null,
    isAuthenticated: true,
    setCurrentThread: vi.fn(),
    addMessageToStore: vi.fn(),
    enableRAG: false,
  };
}


it('clears an approval after the server says the run is no longer awaiting confirmation', async () => {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  let current: ChatPageMessage[] = [];
  const setMessages = vi.fn((m: unknown) => {
    current = typeof m === 'function' ? (m as (p: ChatPageMessage[]) => ChatPageMessage[])(current) : m as ChatPageMessage[];
  });
  streamMessageMock.mockImplementation(async (_r: unknown, cb: Cb) => {
    cb.onConfirmation('agent-thread-1', {tools:[{name:'create_project',args:{name:'audit'}}]});
  });
  streamConfirmMock.mockImplementation(async (_r: unknown, cb: Cb) => {
    cb.onError('Run is not awaiting confirmation', 'conflict');
  });
  const params = makeParams(setMessages);
  useChatStore.setState({refreshMessages: vi.fn().mockResolvedValue(true)});
  const { result } = renderHook(() => useChatStreaming(params), {wrapper});
  await act(async()=>{ await result.current.handleSubmit('create project'); });
  expect(result.current.pendingConfirmation).not.toBeNull();
  await act(async()=>{ await result.current.handleConfirmation(true); });
  expect(result.current.pendingConfirmation).toBeNull();
});
