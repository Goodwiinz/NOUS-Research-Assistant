// After a full reload a pending Codex command-approval card must be restored.
// The cold-load probe replays the run ledger (status accepted first, then
// approval_required); it used to bail on the first status frame and had no
// approval callback, so the card never came back.
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    streamConfirm: vi.fn(),
    resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
    cancelPendingConfirmation: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const loadApproval = vi.fn();
const harnessConnection = {
  executionProvider: 'codex',
  deviceId: 'device-1',
  workspaceId: 'workspace-1',
  devices: [],
  workspaces: [],
  pendingRequests: [],
  connectionState: 'idle',
  statusLabel: 'Codex ready',
  disabledReason: null,
  canSend: true,
  runId: null,
  loadApproval: (...args: unknown[]) => loadApproval(...args),
  receive: vi.fn(),
  markConnectionLost: vi.fn(),
};
vi.mock('@/hooks/chat/useHarnessConnection', () => ({
  useHarnessConnection: () => harnessConnection,
}));

function makeParams(): UseChatStreamingParams {
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
  };
}

describe('useChatStreaming Codex approval probe (cold thread load)', () => {
  beforeEach(() => {
    resumeStreamMock.mockReset();
    loadApproval.mockReset();
    loadApproval.mockResolvedValue({ id: 'req-1' });
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('restores a pending native approval replayed after an accepted status', async () => {
    resumeStreamMock.mockImplementation(
      async (
        _threadId: string,
        _after: number,
        cb: {
          onStatus?: (phase: string, detail?: string) => void;
          onApprovalRequired?: (requestId: string) => void;
        }
      ) => {
        cb.onStatus?.('accepted');
        cb.onApprovalRequired?.('req-1');
        return { status: 'resumed' };
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams();
    renderHook(() => useChatStreaming(params), { wrapper });

    await waitFor(() =>
      expect(loadApproval).toHaveBeenCalledWith('req-1', 'thread-A')
    );
    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
  });
});
