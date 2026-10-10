import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
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
const streamConfirmMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: (...args: unknown[]) => streamConfirmMock(...args),
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

import { useChatStreaming } from '@/hooks/chat/useChatStreaming';

type StreamCallbacks = {
  onToken: (t: string) => void;
  onToolStart: (
    tool: string,
    args: Record<string, unknown>,
    callId?: string
  ) => void;
  onToolEnd: (
    tool: string,
    result: string,
    isError: boolean,
    callId?: string
  ) => void;
  onDone: (p?: unknown) => void;
};

function makeParams() {
  useChatStore.setState({ currentThreadId: 'thread-A' });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
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

describe('useChatStreaming main-stream done tool_executions', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
  });

  it('commits tools from the done payload even when no live tool frames arrived', async () => {
    // Reproduces "tools only render after refresh": the wire delivered tokens
    // but NO tool_start/tool_end frames — the committed bubble must still show
    // tools, reconciled from the graph-final executions in the done payload.
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onToken('here is the answer');
        cb.onDone({
          tool_executions: [
            {
              id: 't1',
              tool_name: 'search_documents',
              tool_display_name: 'Search Documents',
              args: { query: 'photosynthesis' },
              status: 'completed',
              result: { message: 'found 3 docs' },
              duration_ms: 4321,
            },
          ],
        });
        return Promise.resolve();
      }
    );

    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('tell me about photosynthesis');
    });

    const calls = params.setMessages.mock.calls;
    const lastArg = calls[calls.length - 1][0] as ChatPageMessage[];
    const committed = lastArg[lastArg.length - 1];
    expect(committed).toMatchObject({
      role: 'assistant',
      content: 'here is the answer',
      toolExecutions: [
        { tool: 'search_documents', status: 'done', durationMs: 4321 },
      ],
      metadata: { toolsUsed: ['Search documents'] },
    });
  });

  it('correlates concurrent calls to the same tool when they finish out of order', async () => {
    let afterFirstEnd: unknown[] = [];
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onToolStart('search_documents', { query: 'first' }, 'call-a');
        cb.onToolStart('search_documents', { query: 'second' }, 'call-b');
        cb.onToolEnd('search_documents', 'first result', false, 'call-a');
        afterFirstEnd = [...useChatStore.getState().streamingSteps];
        cb.onToolEnd('search_documents', 'second result', false, 'call-b');
        cb.onToken('both searches finished');
        cb.onDone({});
        return Promise.resolve();
      }
    );

    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('compare two searches');
    });

    expect(afterFirstEnd).toMatchObject([
      {
        id: 'call-a',
        args: { query: 'first' },
        status: 'done',
        resultSummary: 'first result',
      },
      { id: 'call-b', args: { query: 'second' }, status: 'running' },
    ]);
    const arrays = params.setMessages.mock.calls
      .map((call) => call[0])
      .filter((value): value is ChatPageMessage[] => Array.isArray(value));
    const committed = arrays[arrays.length - 1].at(-1);
    expect(committed?.toolExecutions).toMatchObject([
      { id: 'call-a', args: { query: 'first' }, result: 'first result' },
      { id: 'call-b', args: { query: 'second' }, result: 'second result' },
    ]);
  });

  it('invalidates the project returned by a completed revision', async () => {
    const invalidate = vi
      .spyOn(QueryClient.prototype, 'invalidateQueries')
      .mockResolvedValue(undefined);
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks) => {
        cb.onToolStart('revise_draft', {});
        cb.onToolEnd(
          'revise_draft',
          '{"status":"completed","project_id":"project-from-result"}',
          false
        );
        cb.onDone({});
        return Promise.resolve();
      }
    );

    try {
      const params = makeParams();
      const { result } = renderHook(() => useChatStreaming(params), {
        wrapper,
      });

      await act(async () => {
        await result.current.handleSubmit('revise the draft');
      });

      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['project', 'project-from-result'],
      });
    } finally {
      invalidate.mockRestore();
    }
  });
});
