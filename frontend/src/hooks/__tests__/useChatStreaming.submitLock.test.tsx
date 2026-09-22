/**
 * submitLockRef single-flight guard (recon `chatfe.guards` — "SUBMIT
 * single-flight"): useChatStreaming.ts stamps a synchronous ref at the top
 * of handleSubmit so a second call arriving before the first has released
 * the lock (fast double Enter / composer re-submit in the same tick) is a
 * no-op, mirroring the CX1 confirmLockRef belt tested in
 * useChatStreaming.confirmToolSteps.test.tsx. No existing test exercised
 * this path — added per the Task 5.5 mutation-verification sweep.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  createElement,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import {
  selectDisplayedMessages,
  type ChatPageMessage,
} from '@/components/chat/shared/cloudMessageView';
import { ChatSurface } from '@/components/chat/ChatSurface';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import type { UseChatSessionReturn } from '@/hooks/chat/useChatSession';
import type { UseChatThreadActionsReturn } from '@/hooks/chat/useChatThreadActions';
import type { UseChatDrawerReturn } from '@/hooks/chat/useChatDrawer';
import type { UseCitationPanelReturn } from '@/hooks/chat/useCitationPanel';
import type { UseChatComposerActionsReturn } from '@/hooks/chat/useChatComposerActions';
import type { UseSlashCommandsReturn } from '@/hooks/chat/useSlashCommands';

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

const streamMessageMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: vi.fn(),
    // The hook probes for a parked HITL confirmation on thread activation;
    // nothing is parked in these scenarios.
    resumeStream: vi.fn().mockResolvedValue({ status: 'idle' }),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    getOrCreateDefaultWorkspace: vi.fn().mockResolvedValue({ id: 'ws-A' }),
    getOrCreateDefaultConversation: vi
      .fn()
      .mockResolvedValue({ id: 'conversation-A' }),
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { workspaceService } from '@/services/workspaceService';

const USER_A = { id: 'user-A' } as NonNullable<
  ReturnType<typeof useAuthStore.getState>['user']
>;
const USER_B = { id: 'user-B' } as NonNullable<
  ReturnType<typeof useAuthStore.getState>['user']
>;

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function makeParams(): UseChatStreamingParams {
  useChatStore.setState({ currentThreadId: 'thread-A' });
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

function OptimisticFirstSendHarness(): ReactElement {
  const [messages, setMessages] = useState<ChatPageMessage[]>([]);
  const [conversations, setConversations] = useState<
    UseChatStreamingParams['conversations']
  >([]);
  const displayedMessages = selectDisplayedMessages({
    localMessages: messages,
    storeMessages: [],
  });
  const streaming = useChatStreaming({
    messages,
    displayedMessages,
    setMessages,
    conversations,
    setConversations,
    dbConversation: null,
    workspace: { id: 'ws-A', name: 'Research' } as never,
    enableRAG: false,
    navigateToThread: () => {},
  });
  const drawerRef = useRef<HTMLDivElement>(null);

  return (
    <ChatSurface
      session={
        {
          conversations,
          workspace: { id: 'ws-A', name: 'Research' },
          activeThreadId: null,
          displayedMessages,
          isAuthenticated: true,
          isInitializing: false,
          initError: null,
          isLoadingMessages: false,
          hasMoreThreads: false,
          loadMoreThreads: () => Promise.resolve(),
          loadOlderMessages: () => Promise.resolve(),
          messagePagination: null,
        } as unknown as UseChatSessionReturn
      }
      streaming={streaming}
      threadActions={
        {
          renameDialog: {
            open: false,
            threadId: '',
            currentTitle: '',
            value: '',
          },
          setRenameDialog: () => {},
          deleteDialog: { open: false, threadId: '' },
          setDeleteDialog: () => {},
          bulkDeleteDialog: { open: false, ids: [] },
          setBulkDeleteDialog: () => {},
          handleRenameThread: () => Promise.resolve(),
          commitRename: () => Promise.resolve(),
          handleDeleteThread: () => {},
          commitDeleteThread: () => Promise.resolve(),
          handleBulkDeleteThreads: () => {},
          commitBulkDelete: () => Promise.resolve(),
        } as UseChatThreadActionsReturn
      }
      drawer={
        {
          isOpen: false,
          openDrawer: () => {},
          closeDrawer: () => {},
          toggleDrawer: () => {},
          drawerRef,
          handleDrawerKeyDown: () => {},
        } as UseChatDrawerReturn
      }
      citationPanel={
        { handleCitationClick: () => {} } as UseCitationPanelReturn
      }
      composerActions={
        {
          handleAttach: async () => [],
          handleRegenerate: () => {},
          handleEditUserMessage: () => {},
        } as unknown as UseChatComposerActionsReturn
      }
      slashCommands={
        {
          commandOutputs: [],
          handleSlashCommand: () => {},
          handleCommandItemAction: () => {},
          submitMessage: () => true,
          startNewChat: () => {},
        } as UseSlashCommandsReturn
      }
      enableRAG={false}
      setEnableRAG={() => {}}
      onSelectThread={() => {}}
    />
  );
}

describe('useChatStreaming submit single-flight (submitLockRef)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    streamMessageMock.mockReset();
    vi.mocked(workspaceService.getOrCreateDefaultWorkspace).mockResolvedValue({
      id: 'ws-A',
    } as never);
    vi.mocked(
      workspaceService.getOrCreateDefaultConversation
    ).mockResolvedValue({ id: 'conversation-A' } as never);
    Element.prototype.scrollIntoView = vi.fn();
    useChatStore.getState().reset();
    useAuthStore.setState({
      user: USER_A,
      organization: null,
      isAuthenticated: true,
      isLoading: false,
      error: null,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the optimistic first message before conversation preflight resolves', async () => {
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    vi.mocked(
      workspaceService.getOrCreateDefaultConversation
    ).mockImplementationOnce(() => new Promise(() => {}) as never);
    vi.mocked(workspaceService.createThread).mockResolvedValue({
      id: 'thread-new',
      conversation_id: 'conversation-A',
      title: 'Visible before preflight',
    } as never);
    streamMessageMock.mockResolvedValue(undefined);
    useChatStore.setState({
      currentThreadId: null,
      isStreaming: false,
      streamingThreadId: null,
    });

    render(<OptimisticFirstSendHarness />, { wrapper });
    fireEvent.change(
      screen.getByPlaceholderText(
        'Ask anything, or paste a passage to discuss…'
      ),
      { target: { value: 'Visible before preflight' } }
    );
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(
        workspaceService.getOrCreateDefaultConversation
      ).toHaveBeenCalledOnce()
    );
    await waitFor(() =>
      expect(screen.getByText('Visible before preflight')).toBeVisible()
    );
    expect(screen.getAllByText('Visible before preflight')).toHaveLength(1);
    expect(workspaceService.createThread).not.toHaveBeenCalled();
    expect(streamMessageMock).not.toHaveBeenCalled();
    expect(
      consoleError.mock.calls.some((args) =>
        args
          .map(String)
          .join(' ')
          .includes('useClientLookup: Index 0 out of bounds')
      )
    ).toBe(true);
    expect(
      consoleError.mock.calls
        .map((args) => args.map(String).join(' '))
        .filter(
          (message) =>
            !/useClientLookup: Index 0 out of bounds|The above error occurred/i.test(
              message
            )
        )
    ).toEqual([]);
  });

  it('keeps drafts isolated by thread, account, and workspace', async () => {
    const params = makeParams();
    const { result, rerender } = renderHook(
      ({ workspaceId }: { workspaceId: string }) =>
        useChatStreaming({
          ...params,
          workspace: { id: workspaceId, name: workspaceId } as never,
        }),
      { initialProps: { workspaceId: 'workspace-A' }, wrapper }
    );

    act(() => result.current.setInput('thread A draft'));
    act(() => useChatStore.setState({ currentThreadId: 'thread-B' }));
    await waitFor(() => expect(result.current.input).toBe(''));

    act(() => result.current.setInput('thread B draft'));
    act(() => useChatStore.setState({ currentThreadId: 'thread-A' }));
    await waitFor(() => expect(result.current.input).toBe('thread A draft'));

    act(() => useAuthStore.setState({ user: USER_B }));
    await waitFor(() => expect(result.current.input).toBe(''));

    act(() => result.current.setInput('account B draft'));
    rerender({ workspaceId: 'workspace-B' });
    await waitFor(() => expect(result.current.input).toBe(''));

    act(() => result.current.setInput('workspace B draft'));
    rerender({ workspaceId: 'workspace-A' });
    await waitFor(() => expect(result.current.input).toBe('account B draft'));
  });

  it('a synchronous second handleSubmit call while the first is still in flight only fires streamMessage once', async () => {
    let releaseStream!: () => void;
    streamMessageMock.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          releaseStream = resolve;
        })
    );

    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), {
      wrapper,
    });

    // Two handleSubmit calls in the same tick, before the first
    // streamMessage call has resolved — mirrors a fast double Enter/click.
    // The second call must be blocked client-side (submitLockRef).
    let p1!: Promise<void>;
    let p2!: Promise<void>;
    act(() => {
      p1 = result.current.handleSubmit('first message');
      p2 = result.current.handleSubmit('second message');
    });

    expect(streamMessageMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      releaseStream();
      await Promise.all([p1, p2]);
    });

    // Still exactly one call after both promises settle.
    expect(streamMessageMock).toHaveBeenCalledTimes(1);
    // The lock released so a later, legitimate submit isn't stuck.
    expect(result.current.isLoading).toBe(false);

    // Prove the lock actually releases: a genuine subsequent submit reaches
    // streamMessage again, rather than relying on the isLoading flag alone.
    streamMessageMock.mockResolvedValueOnce(undefined);
    await act(async () => {
      await result.current.handleSubmit('later message');
    });
    expect(streamMessageMock).toHaveBeenCalledTimes(2);
  });

  it('does not start streaming when Stop lands during first-thread creation', async () => {
    let finishThreadCreation!: (thread: unknown) => void;
    vi.mocked(workspaceService.createThread).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishThreadCreation = resolve;
        }) as never
    );
    const originalMessages: ChatPageMessage[] = [];
    const params = {
      ...makeParams(),
      messages: originalMessages,
      displayedMessages: originalMessages,
      dbConversation: { id: 'conversation-A' } as never,
    };
    useChatStore.setState({
      currentThreadId: null,
      isStreaming: false,
      streamingThreadId: null,
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.handleSubmit('cancel this turn');
    });
    expect(workspaceService.createThread).toHaveBeenCalledOnce();

    act(() => result.current.handleStop());
    expect(params.setMessages).toHaveBeenLastCalledWith(originalMessages);
    expect(result.current.input).toBe('cancel this turn');
    expect(result.current.isLoading).toBe(false);
    expect(streamMessageMock).not.toHaveBeenCalled();

    await act(async () => {
      finishThreadCreation({ id: 'thread-new', title: 'cancel this turn' });
      await submission;
    });

    expect(streamMessageMock).not.toHaveBeenCalled();
    expect(params.setConversations).not.toHaveBeenCalled();
    expect(useChatStore.getState().currentThreadId).toBeNull();
  });

  it('does not continue setup when Stop lands during conversation lookup', async () => {
    let finishConversationLookup!: (conversation: unknown) => void;
    vi.mocked(
      workspaceService.getOrCreateDefaultConversation
    ).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishConversationLookup = resolve;
        }) as never
    );
    const params = makeParams();
    useChatStore.setState({ currentThreadId: null });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.handleSubmit('cancel setup');
    });
    await act(async () => Promise.resolve());
    expect(
      workspaceService.getOrCreateDefaultConversation
    ).toHaveBeenCalledOnce();

    act(() => result.current.handleStop());
    await act(async () => {
      finishConversationLookup({ id: 'conversation-A' });
      await submission;
    });

    expect(workspaceService.createThread).not.toHaveBeenCalled();
    expect(streamMessageMock).not.toHaveBeenCalled();
    expect(result.current.input).toBe('cancel setup');
    expect(result.current.isLoading).toBe(false);
  });

  it.each([
    ['workspace', 'resolve'],
    ['workspace', 'reject'],
    ['conversation', 'resolve'],
    ['conversation', 'reject'],
    ['thread', 'resolve'],
    ['thread', 'reject'],
  ] as const)(
    'abandons user A preflight after the %s await %s without touching user B state',
    async (boundary, outcome) => {
      const pending = deferred<unknown>();
      const navigateToThread = vi.fn();
      const accountAMessage: ChatPageMessage = {
        runtimeId: 'account-A-history',
        source: 'db',
        role: 'user',
        content: 'account A history',
        timestamp: 1,
      };
      const accountBMessage: ChatPageMessage = {
        runtimeId: 'account-B-history',
        source: 'db',
        role: 'user',
        content: 'account B history',
        timestamp: 2,
      };
      const accountBConversation = {
        id: 'thread-B',
        title: 'Account B thread',
        messages: [],
        createdAt: 2,
        updatedAt: 2,
        threadId: 'thread-B',
        conversationId: 'conversation-B',
      };

      vi.mocked(workspaceService.getOrCreateDefaultWorkspace).mockResolvedValue(
        {
          id: 'workspace-A',
        } as never
      );
      vi.mocked(
        workspaceService.getOrCreateDefaultConversation
      ).mockResolvedValue({ id: 'conversation-A' } as never);
      vi.mocked(workspaceService.createThread).mockResolvedValue({
        id: 'thread-A-new',
        conversation_id: 'conversation-A',
        title: 'Account A thread',
      } as never);

      if (boundary === 'workspace') {
        vi.mocked(
          workspaceService.getOrCreateDefaultWorkspace
        ).mockReturnValueOnce(pending.promise as never);
      } else if (boundary === 'conversation') {
        vi.mocked(
          workspaceService.getOrCreateDefaultConversation
        ).mockReturnValueOnce(pending.promise as never);
      } else {
        vi.mocked(workspaceService.createThread).mockReturnValueOnce(
          pending.promise as never
        );
      }

      useChatStore.setState({ currentThreadId: null });
      const { result } = renderHook(
        () => {
          const [messages, setMessages] = useState<ChatPageMessage[]>([
            accountAMessage,
          ]);
          const [conversations, setConversations] = useState<
            UseChatStreamingParams['conversations']
          >([]);
          const streaming = useChatStreaming({
            messages,
            displayedMessages: messages,
            setMessages,
            conversations,
            setConversations,
            dbConversation:
              boundary === 'thread'
                ? ({
                    id: 'conversation-A',
                    workspace_id: 'workspace-A',
                  } as never)
                : null,
            workspace:
              boundary === 'conversation'
                ? ({ id: 'workspace-A' } as never)
                : null,
            enableRAG: false,
            navigateToThread,
          });
          return {
            streaming,
            messages,
            setMessages,
            conversations,
            setConversations,
          };
        },
        { wrapper }
      );

      let submission!: Promise<void>;
      act(() => {
        submission = result.current.streaming.handleSubmit(
          'account A private prompt'
        );
      });
      await waitFor(() => {
        const call =
          boundary === 'workspace'
            ? workspaceService.getOrCreateDefaultWorkspace
            : boundary === 'conversation'
              ? workspaceService.getOrCreateDefaultConversation
              : workspaceService.createThread;
        expect(call).toHaveBeenCalledOnce();
      });

      act(() => {
        useAuthStore.setState({ user: USER_B, isAuthenticated: true });
        useChatStore.setState({ currentThreadId: 'thread-B' });
        result.current.setMessages([accountBMessage]);
        result.current.setConversations([accountBConversation]);
        result.current.streaming.setInput('account B draft');
      });

      await act(async () => {
        if (outcome === 'resolve') {
          pending.resolve(
            boundary === 'workspace'
              ? { id: 'workspace-A' }
              : boundary === 'conversation'
                ? { id: 'conversation-A' }
                : {
                    id: 'thread-A-new',
                    conversation_id: 'conversation-A',
                    title: 'Account A thread',
                  }
          );
        } else {
          pending.reject(new Error(`account A ${boundary} failed`));
        }
        await submission;
      });

      expect(result.current.streaming.input).toBe('account B draft');
      expect(result.current.messages).toEqual([accountBMessage]);
      expect(result.current.conversations).toEqual([accountBConversation]);
      expect(useChatStore.getState().currentThreadId).toBe('thread-B');
      expect(useAuthStore.getState()).toEqual(
        expect.objectContaining({ user: USER_B, isAuthenticated: true })
      );
      expect(navigateToThread).not.toHaveBeenCalled();
      expect(streamMessageMock).not.toHaveBeenCalled();

      if (boundary === 'workspace') {
        expect(
          workspaceService.getOrCreateDefaultConversation
        ).not.toHaveBeenCalled();
      }
      if (boundary !== 'thread') {
        expect(workspaceService.createThread).not.toHaveBeenCalled();
      }
    }
  );

  it('keeps the originating draft when a superseded preflight settles late', async () => {
    const pendingWorkspace = deferred<{ id: string }>();
    vi.mocked(workspaceService.getOrCreateDefaultWorkspace).mockReturnValueOnce(
      pendingWorkspace.promise as never
    );
    const params = makeParams();
    useChatStore.setState({
      currentThreadId: null,
      isStreaming: false,
      streamingThreadId: null,
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.handleSubmit('originating draft');
    });
    await waitFor(() =>
      expect(
        workspaceService.getOrCreateDefaultWorkspace
      ).toHaveBeenCalledOnce()
    );

    act(() => {
      useAuthStore.setState({ user: USER_B });
      useChatStore.setState({ currentThreadId: 'thread-B' });
      result.current.setInput('account B draft');
    });
    expect(result.current.input).toBe('account B draft');

    act(() => result.current.handleStop());
    await act(async () => {
      pendingWorkspace.resolve({ id: 'workspace-A' });
      await submission;
    });

    expect(result.current.input).toBe('account B draft');
    expect(streamMessageMock).not.toHaveBeenCalled();

    act(() => {
      useAuthStore.setState({ user: USER_A });
      useChatStore.setState({ currentThreadId: null });
    });
    await waitFor(() => expect(result.current.input).toBe('originating draft'));
  });

  it('keeps the originating draft when the same account changes threads mid-preflight', async () => {
    const pendingWorkspace = deferred<{ id: string }>();
    vi.mocked(workspaceService.getOrCreateDefaultWorkspace).mockReturnValueOnce(
      pendingWorkspace.promise as never
    );
    const params = makeParams();
    useChatStore.setState({
      currentThreadId: null,
      isStreaming: false,
      streamingThreadId: null,
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.handleSubmit('originating draft');
    });
    await waitFor(() =>
      expect(
        workspaceService.getOrCreateDefaultWorkspace
      ).toHaveBeenCalledOnce()
    );

    act(() => {
      useChatStore.setState({ currentThreadId: 'thread-B' });
      result.current.setInput('thread B draft');
    });
    expect(result.current.input).toBe('thread B draft');

    act(() => result.current.handleStop());
    await act(async () => {
      pendingWorkspace.resolve({ id: 'workspace-A' });
      await submission;
    });

    expect(result.current.input).toBe('thread B draft');
    expect(streamMessageMock).not.toHaveBeenCalled();

    act(() => useChatStore.setState({ currentThreadId: null }));
    await waitFor(() => expect(result.current.input).toBe('originating draft'));
  });

  it('does not resurrect a sent first-chat prompt after starting New chat', async () => {
    vi.mocked(workspaceService.createThread).mockResolvedValueOnce({
      id: 'thread-created',
      conversation_id: 'conversation-A',
      title: 'Sent prompt',
    } as never);
    streamMessageMock.mockResolvedValue(undefined);
    const params = {
      ...makeParams(),
      workspace: { id: 'workspace-A' } as never,
      dbConversation: { id: 'conversation-A' } as never,
    };
    useChatStore.setState({
      currentThreadId: null,
      isStreaming: false,
      streamingThreadId: null,
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('already sent');
    });
    expect(streamMessageMock).toHaveBeenCalledOnce();

    act(() => useChatStore.setState({ currentThreadId: null }));
    await waitFor(() => expect(result.current.input).toBe(''));
  });
});
