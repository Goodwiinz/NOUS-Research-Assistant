// The Codex picker filters computers and folders by the chat's project, so
// useChatStreaming must hand useHarnessConnection the same project binding it
// uses for page_context: the thread row first, ?projectId= only while the
// thread has not loaded, and null for a loaded chat with no project.
// Command: pnpm --dir frontend exec vitest run
// src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import { createElement, type ReactElement, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import type { Thread } from '@/types/workspace';
import { ThreadStatus } from '@/types/workspace';

const mocks = vi.hoisted(() => ({
  search: new URLSearchParams(),
  harnessArgs: [] as unknown[][],
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => mocks.search,
}));

vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    streamConfirm: vi.fn(),
    resumeStream: vi.fn(async () => ({ status: 'idle' })),
    cancelPendingConfirmation: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    listMessages: vi.fn(async () => ({ messages: [], has_more: false })),
  },
}));

const harnessConnection = {
  executionProvider: 'nous',
  deviceId: null,
  workspaceId: null,
  devices: [],
  workspaces: [],
  pendingRequests: [],
  connectionState: 'idle',
  statusLabel: 'NOUS',
  disabledReason: null,
  canSend: true,
  runId: null,
  loadApproval: vi.fn(),
  receive: vi.fn(),
  markConnectionLost: vi.fn(),
};
vi.mock('@/hooks/chat/useHarnessConnection', () => ({
  useHarnessConnection: (...args: unknown[]) => {
    mocks.harnessArgs.push(args);
    return harnessConnection;
  },
}));

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

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

const iso = new Date().toISOString();
const makeThread = (id: string, extra: Partial<Thread> = {}): Thread => ({
  id,
  conversation_id: 'conv-1',
  title: id,
  status: ThreadStatus.ACTIVE,
  last_message_at: iso,
  message_count: 0,
  token_count: 0,
  created_at: iso,
  updated_at: iso,
  ...extra,
});

async function renderStreaming(): Promise<void> {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  renderHook(() => useChatStreaming(makeParams()), { wrapper });
}

describe('useChatStreaming gives the Codex picker the chat project', () => {
  beforeEach(() => {
    mocks.harnessArgs.length = 0;
    mocks.search = new URLSearchParams();
    useChatStore.setState({
      currentThreadId: null,
      threads: {},
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it("passes the thread's project", async () => {
    useChatStore.setState({
      currentThreadId: 'thread-A',
      threads: {
        'conv-1': [makeThread('thread-A', { source_project_id: 'project-1' })],
      },
    });
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual(['thread-A', 'project-1']);
  });

  it('passes null for a loaded chat with no project, ignoring a stale ?projectId=', async () => {
    mocks.search = new URLSearchParams('projectId=stale');
    useChatStore.setState({
      currentThreadId: 'thread-A',
      threads: { 'conv-1': [makeThread('thread-A')] },
    });
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual(['thread-A', null]);
  });

  it('passes the ?projectId= intent while the chat has not loaded', async () => {
    mocks.search = new URLSearchParams('projectId=project-2');
    useChatStore.setState({ currentThreadId: 'ghost', threads: {} });
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual(['ghost', 'project-2']);
  });

  it('passes no project for a new chat with no project intent', async () => {
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual([null, undefined]);
  });
});
