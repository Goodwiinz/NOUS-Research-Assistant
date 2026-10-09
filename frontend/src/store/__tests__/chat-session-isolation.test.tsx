/** Actual auth actions, chat stores and widget; only service I/O is mocked. */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuthStore } from '@/stores/authStore';
import { useAgentChatStore } from '@/store/agentChatStore';
import { useChatStore } from '@/store/chat-store';
import { GlobalAgentChat } from '@/components/agent-chat/GlobalAgentChat';
import type { User } from '@/types';
import type { ChatMessage } from '@/types/workspace';
import { workspaceService } from '@/services/workspaceService';
import { agentChatService } from '@/services/agentChatService';
import type { AgentStreamCallbacks } from '@/services/agentChatService';
import { api } from '@/services/api-client';

const mocks = vi.hoisted(() => ({
  auth: {
    onAuthStateChange: vi.fn(),
    signOut: vi.fn().mockResolvedValue({ error: null }),
    signInWithPassword: vi.fn().mockResolvedValue({
      data: { session: { access_token: 'synthetic-audit-token' } },
      error: null,
    }),
  },
  pageContext: { type: 'overview', label: 'Overview' },
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({ auth: mocks.auth }),
}));
vi.mock('@/services/api-client', () => ({
  api: {
    clearAuth: vi.fn(),
    get: vi.fn().mockResolvedValue({
      user: { id: 'user-B' },
      organization: { id: 'org-B' },
    }),
  },
}));
vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: {
    listWorkspaces: vi.fn(),
    listConversations: vi.fn(),
    listThreads: vi.fn(),
    getOrCreateDefaultWorkspace: vi.fn(),
    createThread: vi.fn(),
  },
}));
vi.mock('@/services/agentChatService', () => ({
  isTerminalJobStatus: vi.fn(),
  agentChatService: {
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    streamMessage: vi.fn(),
    startDurableRun: vi.fn(),
    streamConfirm: vi.fn(),
    confirmAction: vi.fn(),
    completeDurableConfirmation: vi.fn(),
  },
}));
vi.mock('@/hooks/usePageContext', () => ({
  usePageContext: () => mocks.pageContext,
}));
vi.mock('next/navigation', () => ({
  usePathname: () => '/',
  useParams: () => ({}),
}));
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));
vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children }: { children: string }) => <pre>{children}</pre>,
}));
vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  oneDark: {},
}));

const privateContent = 'User A synthetic private research conversation';

beforeEach(() => {
  mocks.auth.signOut.mockResolvedValue({ error: null });
  mocks.auth.signInWithPassword.mockResolvedValue({
    data: { session: { access_token: 'synthetic-audit-token' } },
    error: null,
  });
  vi.mocked(api.get).mockResolvedValue({
    user: { id: 'user-B' },
    organization: { id: 'org-B' },
  });
  vi.mocked(agentChatService.listThreads).mockResolvedValue({
    threads: [],
    total: 0,
  });
  useChatStore.getState().reset();
  useAgentChatStore.getState().reset();
  useAuthStore.setState({
    user: { id: 'user-A' } as User,
    isAuthenticated: true,
    isLoading: false,
  });
  useAgentChatStore.setState({
    uiMode: 'panel',
    activeThreadId: 'user-A-thread',
    inputValue: 'User A unsent draft',
    messages: [
      {
        id: 'user-A-message',
        role: 'user',
        content: privateContent,
        timestamp: new Date(),
      },
    ],
  });
  useChatStore.setState({
    currentThreadId: 'user-A-thread',
    messages: {
      'user-A-thread': [
        { id: 'user-A-message', content: privateContent } as ChatMessage,
      ],
    },
  });
  HTMLElement.prototype.scrollTo = vi.fn();
});

describe('account transition isolation', () => {
  it.each(['signOut', 'invalidateRejectedSession', 'switchAccount'] as const)(
    '%s removes user A chats before user B sees the widget',
    async (action) => {
      await act(async () => {
        if (action === 'signOut') await useAuthStore.getState().signOut();
        else if (action === 'invalidateRejectedSession')
          useAuthStore.getState().invalidateRejectedSession('user-A');
        await useAuthStore
          .getState()
          .signIn('user-b@example.invalid', 'synthetic-password');
      });
      expect(useAuthStore.getState().user?.id).toBe('user-B');
      await act(async () => {
        render(<GlobalAgentChat />);
      });

      expect.soft(screen.queryByText(privateContent)).toBeNull();
      expect.soft(useAgentChatStore.getState().messages).toEqual([]);
      expect.soft(useAgentChatStore.getState().inputValue).toBe('');
      expect.soft(useChatStore.getState().messages).toEqual({});
      expect.soft(useChatStore.getState().currentThreadId).toBeNull();
    }
  );

  it.each([
    'workspaces',
    'conversations',
    'threads',
    'bootstrap',
    'createThread',
    'agentThreads',
  ] as const)(
    'discards a previous account’s delayed %s response',
    async (kind) => {
      let finish!: (value: never) => void;
      const response = new Promise<never>((resolve) => {
        finish = resolve;
      });
      const store = useChatStore.getState();
      const operations = {
        workspaces: () => {
          vi.mocked(workspaceService.listWorkspaces).mockReturnValueOnce(
            response
          );
          return store.loadWorkspaces();
        },
        conversations: () => {
          vi.mocked(workspaceService.listConversations).mockReturnValueOnce(
            response
          );
          return store.loadConversations('private-workspace');
        },
        threads: () => {
          vi.mocked(workspaceService.listThreads).mockReturnValueOnce(response);
          return store.loadThreads('private-conversation');
        },
        bootstrap: () => {
          vi.mocked(
            workspaceService.getOrCreateDefaultWorkspace
          ).mockReturnValueOnce(response);
          return store.initializeDefaultWorkspace();
        },
        createThread: () => {
          vi.mocked(workspaceService.createThread).mockReturnValueOnce(
            response
          );
          return store.createThread({
            conversation_id: 'private-conversation',
            title: 'Private title',
          });
        },
        agentThreads: () => {
          vi.mocked(agentChatService.listThreads).mockReturnValueOnce(response);
          return useAgentChatStore.getState().loadThreads();
        },
      };
      const payloads = {
        workspaces: [{ id: 'private-workspace', name: 'Private workspace' }],
        conversations: {
          conversations: [
            { id: 'private-conversation', title: 'Private title' },
          ],
        },
        threads: {
          threads: [{ id: 'private-thread', title: 'Private title' }],
        },
        bootstrap: { id: 'private-workspace', name: 'Private workspace' },
        createThread: {
          id: 'private-thread',
          conversation_id: 'private-conversation',
          title: 'Private title',
        },
        agentThreads: {
          threads: [{ id: 'private-thread', title: 'Private title' }],
        },
      };
      const pending = operations[kind]();
      if (kind === 'agentThreads')
        await vi.waitFor(() =>
          expect(agentChatService.listThreads).toHaveBeenCalled()
        );
      await useAuthStore.getState().signOut();
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
      finish(payloads[kind] as never);
      await pending;
      expect(useChatStore.getState()).toMatchObject({
        workspaces: [],
        currentWorkspaceId: null,
        error: null,
      });
      expect(useChatStore.getState().conversations).toEqual({});
      expect(useChatStore.getState().threads).toEqual({});
      expect(useChatStore.getState().messages).toEqual({});
      expect(useAgentChatStore.getState().threads).toEqual([]);
    }
  );

  it('aborts the old account’s stream and ignores late tokens and completion', async () => {
    let finish!: () => void;
    let callbacks!: AgentStreamCallbacks;
    let signal!: AbortSignal;
    vi.mocked(agentChatService.streamMessage).mockImplementationOnce(
      async (_request, handlers, abortSignal) => {
        callbacks = handlers;
        signal = abortSignal!;
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
      }
    );
    const pending = useAgentChatStore.getState().sendMessage();
    await vi.waitFor(() =>
      expect(agentChatService.streamMessage).toHaveBeenCalled()
    );
    await useAuthStore.getState().signOut();
    expect.soft(signal.aborted).toBe(true);
    callbacks.onToken?.('Private late answer');
    callbacks.onDone?.();
    finish();
    await pending;
    expect(useAgentChatStore.getState()).toMatchObject({
      messages: [],
      activeThreadId: null,
      isStreaming: false,
    });
  });

  it('does not start a request if sign-out happens during the service import', async () => {
    const pending = useAgentChatStore.getState().sendMessage();
    await useAuthStore.getState().signOut();
    await pending;
    expect(agentChatService.streamMessage).not.toHaveBeenCalled();
    expect(agentChatService.startDurableRun).not.toHaveBeenCalled();
    expect(useAgentChatStore.getState().messages).toEqual([]);
  });
});

describe('old agent confirmation completion', () => {
  it.each(['resolve', 'reject'] as const)(
    '%s cannot restore A confirmation or start a fallback as B',
    async (outcome) => {
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const response = new Promise<void>((yes, no) => {
        resolve = yes;
        reject = no;
      });
      vi.mocked(agentChatService.streamConfirm).mockReturnValueOnce(response);
      useAgentChatStore.setState({
        pendingConfirmations: {
          'user-A-thread': {
            threadId: 'user-A-thread',
            assistantMessageId: 'A-confirm',
            jobId: 'A-job',
            origin: 'sse',
            waitTokenId: 'A-token',
            tools: [{ name: 'write', args: { private: privateContent } }],
            message: privateContent,
          },
        },
      });
      const confirmation = useAgentChatStore
        .getState()
        .confirmAction('user-A-thread', true);
      await waitFor(() =>
        expect(agentChatService.streamConfirm).toHaveBeenCalled()
      );
      await useAuthStore.getState().signOut();
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
      if (outcome === 'resolve') resolve();
      else reject(new Error('old stream failed'));
      await confirmation;
      expect(useAgentChatStore.getState().pendingConfirmations).toEqual({});
      expect(useAgentChatStore.getState().messages).toEqual([]);
      expect(agentChatService.confirmAction).not.toHaveBeenCalled();
      expect(
        agentChatService.completeDurableConfirmation
      ).not.toHaveBeenCalled();
    }
  );
});

it('does not start a durable agent run after A streaming transport rejects in B session', async () => {
  let reject!: (error: Error) => void;
  vi.mocked(agentChatService.streamMessage).mockReturnValueOnce(
    new Promise<void>((_, no) => {
      reject = no;
    })
  );
  const pending = useAgentChatStore.getState().sendMessage();
  await waitFor(() =>
    expect(agentChatService.streamMessage).toHaveBeenCalled()
  );
  await useAuthStore.getState().signOut();
  await useAuthStore
    .getState()
    .signIn('user-b@example.invalid', 'synthetic-password');
  reject(new Error('old SSE rejected'));
  await pending;
  expect(agentChatService.startDurableRun).not.toHaveBeenCalled();
  expect(useAgentChatStore.getState().messages).toEqual([]);
});
