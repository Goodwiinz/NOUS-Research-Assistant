/**
 * Characterization tests for ChatPage's slash-command dispatch, the
 * stop/retry/regenerate actions, and the HITL confirmation gating — all
 * currently orchestrated directly inside page.tsx (Task 5.1). These freeze
 * current behavior before Task 5.2 extracts them into dedicated hooks
 * (useSlashCommands / useChatComposerActions).
 *
 * Mocked at the same hook boundary the sibling ChatPage.*.test.tsx files
 * already mock at: useChatSession / useChatStreaming / useChatThreadActions.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPage from '../../../../app/(dashboard)/chat/page';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type {
  CommandAction,
  CommandOutput,
} from '@/components/chat/commandOutput';
import type { PendingConfirmation } from '@/hooks/chat/useChatStreaming';
import {
  getNewChatUrl,
  getSelectedThreadUrl,
} from '@/components/chat/shared/chatNavigation';

const mockUseChatSession = vi.fn();
const mockUseChatStreaming = vi.fn();
const mockConfirmationBelongsToThread = vi.fn();
const mockRouterPush = vi.fn();
const mockRouterReplace = vi.fn();

vi.mock('@/components/chat', () => ({
  ChatInput: (props: {
    onSubmit: () => void;
    onCommand?: (id: string) => void;
    isLoading: boolean;
  }) => (
    <div>
      <span data-testid="input-is-loading">{String(props.isLoading)}</span>
      <button type="button" data-testid="input-submit" onClick={props.onSubmit}>
        Send
      </button>
      {(['new', 'retry', 'clear', 'help', 'threads'] as const).map((id) => (
        <button
          key={id}
          type="button"
          data-testid={`command-${id}`}
          onClick={() => props.onCommand?.(id)}
        >
          /{id}
        </button>
      ))}
    </div>
  ),
  CitationPanel: () => null,
  WelcomeState: () => <div data-testid="welcome-state" />,
}));
vi.mock('@/components/chat/ChatDialogs', () => ({ ChatDialogs: () => null }));
vi.mock('@/components/chat/ChatHeader', () => ({ ChatHeader: () => null }));
vi.mock('@/components/chat/ChatMessageList', () => ({
  ChatMessageList: (props: {
    messages: ChatPageMessage[];
    commandOutputs?: CommandOutput[];
    onCommandItemAction?: (action: CommandAction) => void;
    onRegenerate: (index: number) => void;
  }) => (
    <div>
      {props.messages.map((message) => (
        <p key={`${message.role}:${message.content}`}>{message.content}</p>
      ))}
      <div data-testid="command-outputs">
        {(props.commandOutputs ?? []).map((output) => (
          <div key={output.id}>
            <p data-testid="command-name">{output.command}</p>
            {(output.lines ?? []).map((line, i) => (
              <p key={i}>{line}</p>
            ))}
            {(output.items ?? []).map((item) => (
              <button
                key={item.key}
                type="button"
                data-testid={`command-item-${item.key}`}
                onClick={() =>
                  item.action && props.onCommandItemAction?.(item.action)
                }
              >
                {item.label}
              </button>
            ))}
          </div>
        ))}
      </div>
      <button
        type="button"
        data-testid="regenerate-assistant"
        onClick={() => props.onRegenerate(1)}
      >
        Regenerate
      </button>
    </div>
  ),
}));
vi.mock('@/components/chat/aui/ChatRuntimeProvider', () => ({
  ChatRuntimeProvider: ({
    children,
    onCancel,
    onApproval,
  }: {
    children: React.ReactNode;
    onCancel: () => void;
    onApproval?: (approved: boolean) => void;
  }) => (
    <section>
      <button type="button" data-testid="runtime-cancel" onClick={onCancel}>
        Cancel
      </button>
      <button
        type="button"
        data-testid="runtime-approve"
        onClick={() => onApproval?.(true)}
      >
        Approve
      </button>
      <button
        type="button"
        data-testid="runtime-reject"
        onClick={() => onApproval?.(false)}
      >
        Reject
      </button>
      {children}
    </section>
  ),
}));
vi.mock('@/components/chat/ChatSidebar', () => ({ ChatSidebar: () => null }));
vi.mock('@/hooks/chat/useChatSession', () => ({
  useChatSession: () => mockUseChatSession(),
}));
vi.mock('@/hooks/chat/useChatStreaming', () => ({
  useChatStreaming: () => mockUseChatStreaming(),
  confirmationBelongsToThread: (...args: unknown[]) =>
    mockConfirmationBelongsToThread(...args),
}));
vi.mock('@/hooks/chat/useChatThreadActions', () => ({
  useChatThreadActions: () => ({
    renameDialog: { open: false },
    setRenameDialog: vi.fn(),
    deleteDialog: { open: false },
    setDeleteDialog: vi.fn(),
    bulkDeleteDialog: { open: false },
    setBulkDeleteDialog: vi.fn(),
    handleRenameThread: vi.fn(),
    commitRename: vi.fn(),
    handleDeleteThread: vi.fn(),
    commitDeleteThread: vi.fn(),
    handleBulkDeleteThreads: vi.fn(),
    commitBulkDelete: vi.fn(),
  }),
}));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockRouterPush, replace: mockRouterReplace }),
}));

function fixture(
  id: string,
  role: 'user' | 'assistant',
  content: string,
  timestamp: number
): ChatPageMessage {
  return { role, content, timestamp, runtimeId: id, source: 'canonical' };
}

function pendingConfirmationFor(threadId: string): PendingConfirmation {
  return {
    threadId,
    workspaceThreadId: threadId,
    approvalId: 'approval-1',
    confirmation: { action: 'ingest_document' },
  };
}

describe('ChatPage commands, stop/retry/regenerate, and HITL gating', () => {
  const mockSetInput = vi.fn();
  const mockHandleSubmit = vi.fn();
  const mockHandleStop = vi.fn();
  const mockHandleConfirmation = vi.fn();
  const mockSetCurrentThread = vi.fn();

  const DEFAULT_STREAMING = {
    input: '',
    setInput: mockSetInput,
    isLoading: false,
    handleSubmit: mockHandleSubmit,
    handleStop: mockHandleStop,
    pendingConfirmation: null,
    handleConfirmation: mockHandleConfirmation,
    chatInputRef: { current: null },
    storeIsStreaming: false,
    storeStreamingContent: '',
    storeIsRetrievingRag: false,
    streamingThreadId: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockConfirmationBelongsToThread.mockReturnValue(false);
    mockUseChatStreaming.mockReturnValue(DEFAULT_STREAMING);
    mockUseChatSession.mockReturnValue({
      conversations: [
        { id: 'thread-1', title: 'First thread', updatedAt: 1 },
        { id: 'thread-2', title: 'Second thread', updatedAt: 2 },
      ],
      setConversations: vi.fn(),
      activeConversationId: 'thread-1',
      setActiveConversationId: vi.fn(),
      messages: [],
      setMessages: vi.fn(),
      workspace: { id: 'ws-1', name: 'Workspace' },
      dbConversation: { id: 'conv-1', title: 'Default' },
      isInitializing: false,
      initError: null,
      isLoadingMessages: false,
      isHydratedRef: { current: true },
      currentThreadIdFromStore: 'thread-1',
      setCurrentThread: mockSetCurrentThread,
      storeMessages: null,
      isAuthenticated: true,
      activeThreadId: 'thread-1',
      displayedMessages: [
        fixture('u1', 'user', 'What is RAG?', 1),
        fixture('a1', 'assistant', 'Retrieval-augmented generation…', 2),
      ],
      loadOlderMessages: vi.fn(),
      messagePagination: null,
      hasMoreThreads: false,
      loadMoreThreads: vi.fn(),
      mapDbMessageToUiMessage: vi.fn(),
      loadThreadsFromDb: vi.fn(),
    });
  });

  it('/new starts a fresh chat through the store authority', () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('command-new'));

    expect(mockSetCurrentThread).toHaveBeenCalledWith(null);
    expect(mockRouterPush).toHaveBeenCalledWith(getNewChatUrl());
  });

  it('/retry regenerates the last assistant turn with the correct prior-turn history', async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('command-retry'));

    await waitFor(() =>
      expect(mockHandleSubmit).toHaveBeenCalledWith(
        'What is RAG?',
        [],
        undefined,
        undefined,
        undefined
      )
    );
  });

  it('regenerating a specific assistant message re-sends its prior user turn', async () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('regenerate-assistant'));

    await waitFor(() =>
      expect(mockHandleSubmit).toHaveBeenCalledWith(
        'What is RAG?',
        [],
        undefined,
        undefined,
        undefined
      )
    );
  });

  it('retry and regenerate are no-ops while a turn is already streaming', async () => {
    mockUseChatStreaming.mockReturnValue({
      ...DEFAULT_STREAMING,
      storeIsStreaming: true,
    });
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('command-retry'));
    fireEvent.click(screen.getByTestId('regenerate-assistant'));

    // Give any (incorrectly) deferred submit a chance to fire before asserting.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockHandleSubmit).not.toHaveBeenCalled();
  });

  it('stop is wired through the assistant-ui runtime cancel action', () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('runtime-cancel'));
    expect(mockHandleStop).toHaveBeenCalledTimes(1);
  });

  it('/clear clears ephemeral command output and the composer input', () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('command-help'));
    expect(screen.getByTestId('command-name')).toHaveTextContent('/help');

    fireEvent.click(screen.getByTestId('command-clear'));

    expect(screen.queryByTestId('command-name')).not.toBeInTheDocument();
    expect(mockSetInput).toHaveBeenCalledWith('');
  });

  it('/threads lists the real conversations and an item click routes through the store authority', () => {
    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('command-threads'));

    expect(screen.getByTestId('command-item-thread-2')).toHaveTextContent(
      'Second thread'
    );

    fireEvent.click(screen.getByTestId('command-item-thread-2'));

    expect(mockSetCurrentThread).toHaveBeenCalledWith('thread-2');
    expect(mockRouterPush).toHaveBeenCalledWith(
      getSelectedThreadUrl('thread-2')
    );
  });

  it('locks the composer only when the pending confirmation belongs to the active thread', () => {
    mockUseChatStreaming.mockReturnValue({
      ...DEFAULT_STREAMING,
      pendingConfirmation: pendingConfirmationFor('thread-2'),
    });

    // Belongs to a different thread than the one displayed — not locked.
    mockConfirmationBelongsToThread.mockReturnValue(false);
    const { rerender } = render(<ChatPage />);
    expect(screen.getByTestId('input-is-loading')).toHaveTextContent('false');

    // Same confirmation, now scoped to the active thread — locked.
    mockConfirmationBelongsToThread.mockReturnValue(true);
    rerender(<ChatPage />);
    expect(screen.getByTestId('input-is-loading')).toHaveTextContent('true');
  });

  it('routes runtime approve/reject straight to handleConfirmation', () => {
    mockUseChatStreaming.mockReturnValue({
      ...DEFAULT_STREAMING,
      pendingConfirmation: pendingConfirmationFor('thread-1'),
    });
    mockConfirmationBelongsToThread.mockReturnValue(true);

    render(<ChatPage />);

    fireEvent.click(screen.getByTestId('runtime-approve'));
    expect(mockHandleConfirmation).toHaveBeenCalledWith(true);

    fireEvent.click(screen.getByTestId('runtime-reject'));
    expect(mockHandleConfirmation).toHaveBeenCalledWith(false);
  });

  it('returns focus to the composer once a same-thread confirmation resolves', () => {
    const focus = vi.fn();
    mockUseChatStreaming.mockReturnValue({
      ...DEFAULT_STREAMING,
      pendingConfirmation: pendingConfirmationFor('thread-1'),
      chatInputRef: { current: { focus } },
    });
    mockConfirmationBelongsToThread.mockReturnValue(true);

    const { rerender } = render(<ChatPage />);
    expect(focus).not.toHaveBeenCalled();

    mockUseChatStreaming.mockReturnValue({
      ...DEFAULT_STREAMING,
      pendingConfirmation: null,
      chatInputRef: { current: { focus } },
    });
    mockConfirmationBelongsToThread.mockReturnValue(false);
    rerender(<ChatPage />);

    expect(focus).toHaveBeenCalled();
  });
});
