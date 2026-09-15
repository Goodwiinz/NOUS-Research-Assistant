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
    useChatStore.setState({ currentThreadId: null });

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
    useChatStore.setState({ currentThreadId: null });
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
});
