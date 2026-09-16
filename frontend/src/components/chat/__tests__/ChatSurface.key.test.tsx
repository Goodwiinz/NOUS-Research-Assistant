/**
 * ChatSurface runtime-key contract (audit F2, chat-bug-hunt 2026-08-23).
 *
 * The ChatRuntimeProvider key must encode THREAD IDENTITY ONLY. It used to
 * also encode a hydration phase (`new:empty` → `new:hydrated`), so the
 * moment the first optimistic message landed on a brand-new chat React
 * unmounted and remounted the entire runtime subtree mid-turn — dropping
 * composer focus to <body> and resetting internal runtime state on every
 * new-chat first send.
 *
 * These tests FAIL against the old behavior: the first asserts NO
 * unmount/remount when messages go 0→1 within one thread; the second pins
 * the desired remount on a genuine thread switch so the fix can't be
 * over-applied by removing the key entirely.
 */

import { render } from '@testing-library/react';
import { useEffect, type ComponentProps, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatSurface } from '@/components/chat/ChatSurface';
import { type ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatSessionReturn } from '@/hooks/chat/useChatSession';
import type { UseChatStreamingReturn } from '@/hooks/chat/useChatStreaming';
import type { UseChatThreadActionsReturn } from '@/hooks/chat/useChatThreadActions';
import type { UseChatDrawerReturn } from '@/hooks/chat/useChatDrawer';
import type { UseCitationPanelReturn } from '@/hooks/chat/useCitationPanel';
import type { UseChatComposerActionsReturn } from '@/hooks/chat/useChatComposerActions';
import type { UseSlashCommandsReturn } from '@/hooks/chat/useSlashCommands';

let runtimeMounts = 0;
let runtimeUnmounts = 0;

vi.mock('@/components/chat', () => ({
  ChatInput: () => <div data-testid="chat-input" />,
}));

vi.mock('@/components/chat/aui/ChatRuntimeProvider', () => ({
  ChatRuntimeProvider: ({ children }: { children?: ReactNode }) => {
    useEffect(() => {
      runtimeMounts += 1;
      return () => {
        runtimeUnmounts += 1;
      };
    }, []);
    return <div data-testid="runtime">{children}</div>;
  },
}));

vi.mock('@/components/chat/ChatTranscriptState', () => ({
  ChatTranscriptState: () => <div data-testid="transcript" />,
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

const msg = (runtimeId: string): ChatPageMessage => ({
  runtimeId,
  source: 'canonical',
  role: 'user',
  content: 'hello',
  timestamp: 0,
});

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

const makeProps = (
  session: UseChatSessionReturn
): ComponentProps<typeof ChatSurface> =>
  ({
    session,
    streaming: {
      input: '',
      setInput: vi.fn(),
      isLoading: false,
      handleSubmit: vi.fn(),
      handleStop: vi.fn(),
      pendingConfirmation: null,
      handleConfirmation: vi.fn(),
      chatInputRef: { current: null },
      storeIsStreaming: false,
      storeStreamingContent: '',
      storeIsRetrievingRag: false,
      streamingThreadId: null,
    } as unknown as UseChatStreamingReturn,
    threadActions: {} as unknown as UseChatThreadActionsReturn,
    drawer: {
      isOpen: false,
      closeDrawer: vi.fn(),
      toggleDrawer: vi.fn(),
      drawerRef: { current: null },
      handleDrawerKeyDown: vi.fn(),
    } as unknown as UseChatDrawerReturn,
    citationPanel: {
      handleCitationClick: vi.fn(),
    } as unknown as UseCitationPanelReturn,
    composerActions: {
      handleAttach: vi.fn(),
      handleRegenerate: vi.fn(),
      handleEditUserMessage: vi.fn(),
    } as unknown as UseChatComposerActionsReturn,
    slashCommands: {
      commandOutputs: [],
      handleSlashCommand: vi.fn(),
      handleCommandItemAction: vi.fn(),
      submitMessage: vi.fn(),
      startNewChat: vi.fn(),
    } as unknown as UseSlashCommandsReturn,
    enableRAG: true,
    setEnableRAG: vi.fn(),
    onSelectThread: vi.fn(),
  }) as ComponentProps<typeof ChatSurface>;

describe('ChatSurface runtime key (F2)', () => {
  it('docks history only when there is room for both side panels', () => {
    const { getByTestId } = render(
      <ChatSurface {...makeProps(makeSession())} />
    );
    expect(getByTestId('chat-sidebar').parentElement).toHaveClass(
      'hidden',
      'xl:block'
    );
  });

  beforeEach(() => {
    runtimeMounts = 0;
    runtimeUnmounts = 0;
  });

  it('does NOT remount the runtime when the first message lands on a brand-new chat', () => {
    const initial = makeProps(makeSession());
    const { rerender } = render(<ChatSurface {...initial} />);

    // First optimistic message arrives mid-turn; same (nonexistent) thread id.
    const afterFirstMessage = makeProps(
      makeSession({ displayedMessages: [msg('opt-1')] })
    );
    rerender(<ChatSurface {...afterFirstMessage} />);

    expect(runtimeMounts).toBe(1);
    expect(runtimeUnmounts).toBe(0);
  });

  it('does NOT remount when the server assigns the new chat its thread id', () => {
    const optimistic = {
      ...msg('opt-1'),
      source: 'optimistic' as const,
    };
    const initial = makeProps(makeSession({ displayedMessages: [optimistic] }));
    const { rerender } = render(<ChatSurface {...initial} />);

    const created = makeProps(
      makeSession({
        activeThreadId: 't-created',
        displayedMessages: [optimistic],
      })
    );
    rerender(<ChatSurface {...created} />);

    expect(runtimeMounts).toBe(1);
    expect(runtimeUnmounts).toBe(0);
  });

  it('remounts when starting another new chat after thread-id adoption', () => {
    const optimistic = {
      ...msg('opt-1'),
      source: 'optimistic' as const,
    };
    const initial = makeProps(makeSession({ displayedMessages: [optimistic] }));
    const { rerender } = render(<ChatSurface {...initial} />);

    rerender(
      <ChatSurface
        {...makeProps(
          makeSession({
            activeThreadId: 't-created',
            displayedMessages: [optimistic],
          })
        )}
      />
    );
    rerender(<ChatSurface {...makeProps(makeSession())} />);

    expect(runtimeMounts).toBe(2);
    expect(runtimeUnmounts).toBe(1);
  });

  it('does NOT remount while a turn streams into an existing thread', () => {
    const initial = makeProps(
      makeSession({ activeThreadId: 't-1', displayedMessages: [] })
    );
    const { rerender } = render(<ChatSurface {...initial} />);

    const hydrated = makeProps(
      makeSession({
        activeThreadId: 't-1',
        displayedMessages: [msg('m-1'), msg('m-2')],
      })
    );
    rerender(<ChatSurface {...hydrated} />);

    expect(runtimeMounts).toBe(1);
    expect(runtimeUnmounts).toBe(0);
  });

  it('STILL remounts on a genuine thread switch (key encodes thread identity)', () => {
    const initial = makeProps(
      makeSession({ activeThreadId: 't-1', displayedMessages: [msg('a')] })
    );
    const { rerender } = render(<ChatSurface {...initial} />);

    const switched = makeProps(
      makeSession({ activeThreadId: 't-2', displayedMessages: [msg('b')] })
    );
    rerender(<ChatSurface {...switched} />);

    expect(runtimeMounts).toBe(2);
    expect(runtimeUnmounts).toBe(1);
  });
});
