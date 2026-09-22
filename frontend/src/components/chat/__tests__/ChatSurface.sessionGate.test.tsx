/**
 * ChatSurface session-interactivity gate.
 *
 * Pins the fix for a pre-existing bug preserved by the behavior-freeze store
 * split (PR #1205 CodeRabbit triage, 2026-07-16): the composer stayed fully
 * interactive while the session could not accept a turn (unauthenticated,
 * still initializing, or failed to initialize) — every submission path fired
 * anyway. These tests FAIL against the old behavior.
 */

import { render } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ChatSurface } from '@/components/chat/ChatSurface';
import type { UseChatSessionReturn } from '@/hooks/chat/useChatSession';
import type { UseChatStreamingReturn } from '@/hooks/chat/useChatStreaming';
import type { UseChatThreadActionsReturn } from '@/hooks/chat/useChatThreadActions';
import type { UseChatDrawerReturn } from '@/hooks/chat/useChatDrawer';
import type { UseCitationPanelReturn } from '@/hooks/chat/useCitationPanel';
import type { UseChatComposerActionsReturn } from '@/hooks/chat/useChatComposerActions';
import type { UseSlashCommandsReturn } from '@/hooks/chat/useSlashCommands';

type RuntimeCapture = {
  isSendDisabled?: boolean;
  onSend: (text: string) => void;
  children?: ReactNode;
};
type InputCapture = {
  onSubmit: () => void;
  onCommand?: (id: string) => void;
};
type TranscriptCapture = {
  onRegenerate: (index: number) => void;
  onPromptSelect: (prompt: string) => void;
};

let runtimeProps: RuntimeCapture;
let inputProps: InputCapture;
let transcriptProps: TranscriptCapture;

vi.mock('@/components/chat', () => ({
  ChatInput: (props: InputCapture) => {
    inputProps = props;
    return <div data-testid="chat-input" />;
  },
  CitationPanel: () => <div data-testid="citation-panel" />,
}));

vi.mock('@/components/chat/aui/ChatRuntimeProvider', () => ({
  ChatRuntimeProvider: (props: RuntimeCapture) => {
    runtimeProps = props;
    return <div data-testid="runtime">{props.children}</div>;
  },
}));

vi.mock('@/components/chat/ChatTranscriptState', () => ({
  ChatTranscriptState: (props: TranscriptCapture) => {
    transcriptProps = props;
    return <div data-testid="transcript" />;
  },
}));

vi.mock('@/components/chat/ChatDialogs', () => ({
  ChatDialogs: () => <div data-testid="chat-dialogs" />,
}));

vi.mock('@/components/chat/ChatHeader', () => ({
  ChatHeader: () => <div data-testid="chat-header" />,
}));

vi.mock('@/components/chat/ChatSidebar', () => ({
  ChatSidebar: () => <div data-testid="chat-sidebar" />,
}));

vi.mock('@/hooks/chat/useChatStreaming', () => ({
  confirmationBelongsToThread: () => false,
}));

const makeSession = (
  overrides: Partial<UseChatSessionReturn> = {}
): UseChatSessionReturn =>
  ({
    conversations: [],
    workspace: null,
    activeThreadId: null,
    displayedMessages: [],
    isAuthenticated: true,
    isInitializing: false,
    initError: null,
    isLoadingMessages: false,
    hasMoreThreads: false,
    loadMoreThreads: vi.fn(),
    loadOlderMessages: vi.fn(),
    messagePagination: {},
    ...overrides,
  }) as unknown as UseChatSessionReturn;

type MockFn = ReturnType<typeof vi.fn>;

const makeProps = (
  session: UseChatSessionReturn
): {
  handleSubmit: MockFn;
  submitMessage: MockFn;
  handleSlashCommand: MockFn;
  handleRegenerate: MockFn;
  props: ComponentProps<typeof ChatSurface>;
} => {
  const handleSubmit = vi.fn();
  const submitMessage = vi.fn();
  const handleSlashCommand = vi.fn();
  const handleRegenerate = vi.fn();
  return {
    handleSubmit,
    submitMessage,
    handleSlashCommand,
    handleRegenerate,
    props: {
      session,
      streaming: {
        input: '',
        setInput: vi.fn(),
        isLoading: false,
        handleSubmit,
        handleStop: vi.fn(),
        pendingConfirmation: null,
        handleConfirmation: vi.fn(),
        chatInputRef: { current: null },
        storeIsStreaming: false,
        storeStreamingContent: '',
        storeIsRetrievingRag: false,
        streamingThreadId: null,
      } as unknown as UseChatStreamingReturn,
      threadActions: {
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
      } as unknown as UseChatThreadActionsReturn,
      drawer: {
        isOpen: false,
        closeDrawer: vi.fn(),
        toggleDrawer: vi.fn(),
        drawerRef: { current: null },
        handleDrawerKeyDown: vi.fn(),
      } as unknown as UseChatDrawerReturn,
      citationPanel: {
        isCitationPanelOpen: false,
        setIsCitationPanelOpen: vi.fn(),
        citationPanelCitations: [],
        activeCitationId: null,
        citationTraceId: null,
        handleCitationClick: vi.fn(),
        handleCiteSource: vi.fn(),
      } as unknown as UseCitationPanelReturn,
      composerActions: {
        handleAttach: vi.fn(),
        handleRegenerate,
      } as unknown as UseChatComposerActionsReturn,
      slashCommands: {
        commandOutputs: [],
        handleSlashCommand,
        handleCommandItemAction: vi.fn(),
        submitMessage,
        startNewChat: vi.fn(),
      } as unknown as UseSlashCommandsReturn,
      enableRAG: true,
      setEnableRAG: vi.fn(),
      onSelectThread: vi.fn(),
    },
  };
};

const fireAllSubmissionPaths = (): void => {
  runtimeProps.onSend('hello');
  inputProps.onSubmit();
  inputProps.onCommand?.('new');
  transcriptProps.onRegenerate(0);
};

describe('ChatSurface session-interactivity gate', () => {
  it.each([
    ['unauthenticated', { isAuthenticated: false }],
    ['initializing', { isInitializing: true }],
    ['init error', { initError: 'Failed to initialize chat' }],
  ] as const)(
    'keeps every submission path inert while the session is %s',
    (_label, sessionOverrides) => {
      const {
        handleSubmit,
        submitMessage,
        handleSlashCommand,
        handleRegenerate,
        props,
      } = makeProps(makeSession(sessionOverrides));

      render(<ChatSurface {...props} />);
      fireAllSubmissionPaths();

      expect(runtimeProps.isSendDisabled).toBe(true);
      expect(handleSubmit).not.toHaveBeenCalled();
      expect(submitMessage).not.toHaveBeenCalled();
      expect(handleSlashCommand).not.toHaveBeenCalled();
      expect(handleRegenerate).not.toHaveBeenCalled();
    }
  );

  it('passes every submission path through once the session is interactive', () => {
    const {
      handleSubmit,
      submitMessage,
      handleSlashCommand,
      handleRegenerate,
      props,
    } = makeProps(makeSession());

    render(<ChatSurface {...props} />);
    fireAllSubmissionPaths();

    expect(runtimeProps.isSendDisabled).toBe(false);
    expect(handleSubmit).toHaveBeenCalledWith(
      'hello',
      undefined,
      undefined,
      undefined
    );
    expect(submitMessage).toHaveBeenCalledTimes(1);
    expect(handleSlashCommand).toHaveBeenCalledWith('new');
    expect(handleRegenerate).toHaveBeenCalledWith(0);
  });

  it('treats starter prompts as editable drafts and returns focus to the composer', () => {
    const setInput = vi.fn();
    const textarea = document.createElement('textarea');
    document.body.appendChild(textarea);
    const focusSpy = vi.spyOn(textarea, 'focus');
    const { props } = makeProps(makeSession());
    props.streaming.setInput = setInput;
    props.streaming.chatInputRef = { current: textarea };

    render(<ChatSurface {...props} />);
    transcriptProps.onPromptSelect('Summarize the passage I provide: ');

    expect(focusSpy).toHaveBeenCalled();
    const update = setInput.mock.calls[0][0] as (current: string) => string;
    expect(update('')).toBe('Summarize the passage I provide: ');
    expect(update(' ')).toBe(' ');
    expect(update('draft already here')).toBe('draft already here');
    textarea.remove();
  });
});
