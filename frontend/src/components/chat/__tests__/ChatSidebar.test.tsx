import { describe, expect, it, vi } from 'vitest';
import {
  fireEvent,
  render as testingRender,
  screen,
  waitFor,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactElement, ReactNode } from 'react';
import { threadSearchService } from '@/services/threadSearchService';
import type { ThreadSearchResponse } from '@/types/thread-search';
import type { Workspace } from '@/types/workspace';
import { ChatSidebar } from '../ChatSidebar';

vi.mock('@/services/threadSearchService', () => ({
  threadSearchService: { searchThreads: vi.fn() },
}));

const mockedSearchThreads = vi.mocked(threadSearchService.searchThreads);

function render(ui: ReactElement): ReturnType<typeof testingRender> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return testingRender(ui, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  });
}

const mockConversations = [
  {
    id: 'conv-1',
    title: 'Alpha Chat',
    messages: [{ role: 'user', content: 'Hello world' }],
    threadId: 'thread-1',
    updatedAt: Date.now() - 60000,
    messageCount: 1,
  },
  {
    id: 'conv-2',
    title: 'Beta Discussion',
    messages: [
      {
        role: 'assistant',
        content:
          'This is a longer message that should be truncated in the sidebar preview because it exceeds sixty characters easily',
      },
    ],
    threadId: 'thread-2',
    updatedAt: Date.now() - 3600000,
    messageCount: 1,
  },
  {
    id: 'conv-3',
    title: 'Gamma Query',
    messages: [],
    threadId: 'thread-3',
    updatedAt: Date.now() - 86400000,
    messageCount: 0,
  },
];

describe('ChatSidebar', () => {
  const defaultProps = {
    conversations: mockConversations,
    activeId: null,
    onSelect: vi.fn(),
    onNew: vi.fn(),
  };

  it('renders all conversations', () => {
    render(<ChatSidebar {...defaultProps} />);
    expect(screen.getByText('Alpha Chat')).toBeInTheDocument();
    expect(screen.getByText('Beta Discussion')).toBeInTheDocument();
    expect(screen.getByText('Gamma Query')).toBeInTheDocument();
  });

  it('shows source and turn counts when the thread carries a citation count', () => {
    render(
      <ChatSidebar
        {...defaultProps}
        conversations={[
          {
            id: 'conv-cited',
            title: 'Cited Thread',
            messages: [],
            threadId: 'thread-cited',
            updatedAt: Date.now(),
            messageCount: 4,
            citationCount: 3,
          },
        ]}
      />
    );
    expect(screen.getByText('3 sources · 4 turns')).toBeInTheDocument();
  });

  it('keeps the preview snippet for threads without a citation count', () => {
    render(<ChatSidebar {...defaultProps} />);
    expect(screen.getByText('Hello world')).toBeInTheDocument();
  });

  it('shows correct count badge', () => {
    render(<ChatSidebar {...defaultProps} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('calls onNew when New chat button is clicked', () => {
    render(<ChatSidebar {...defaultProps} />);
    fireEvent.click(screen.getByText('New chat'));
    expect(defaultProps.onNew).toHaveBeenCalledTimes(1);
  });

  it('calls onSelect when a conversation is clicked', () => {
    render(<ChatSidebar {...defaultProps} />);
    fireEvent.click(screen.getByText('Alpha Chat'));
    expect(defaultProps.onSelect).toHaveBeenCalledWith('conv-1');
  });

  it('filters conversations by search query', () => {
    render(<ChatSidebar {...defaultProps} />);
    const searchInput = screen.getByPlaceholderText('Search threads...');
    fireEvent.change(searchInput, { target: { value: 'alpha' } });

    expect(screen.getByText('Alpha Chat')).toBeInTheDocument();
    expect(screen.queryByText('Beta Discussion')).not.toBeInTheDocument();
    expect(screen.queryByText('Gamma Query')).not.toBeInTheDocument();
  });

  it('trims surrounding whitespace before matching a thread search', () => {
    render(<ChatSidebar {...defaultProps} />);
    const searchInput = screen.getByPlaceholderText('Search threads...');
    fireEvent.change(searchInput, { target: { value: '  Alpha Chat  ' } });

    expect(screen.getByText('Alpha Chat')).toBeInTheDocument();
    expect(screen.queryByText('Beta Discussion')).not.toBeInTheDocument();
  });

  it('renders an older server hit, selects it, and restores the normal list on clear', async () => {
    mockedSearchThreads.mockResolvedValueOnce({
      query: 'older retrieval',
      search_id: 'search-1',
      results: [
        {
          thread_id: 'thread-older',
          title: 'Older retrieval thread',
          summary: 'A persisted summary from an older page',
          status: 'active',
          conversation_id: 'conversation-1',
          relevance_score: 1,
          message_count: 3,
          last_message_at: '2026-09-20T12:00:00Z',
          created_at: '2026-09-20T11:00:00Z',
          matching_message_count: 1,
        },
      ],
      total_results: 1,
      returned_results: 1,
      search_time_ms: 1,
      limit: 20,
      offset: 0,
      has_more: false,
    } satisfies ThreadSearchResponse);
    const onSelect = vi.fn();

    render(
      <ChatSidebar
        {...defaultProps}
        onSelect={onSelect}
        currentWorkspace={{ id: 'workspace-1', name: 'Workspace' } as Workspace}
      />
    );
    fireEvent.change(screen.getByPlaceholderText('Search threads...'), {
      target: { value: 'older retrieval' },
    });

    await waitFor(() =>
      expect(screen.getByText('Older retrieval thread')).toBeInTheDocument()
    );
    expect(
      screen.getByText('A persisted summary from an older page')
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText('Older retrieval thread'));
    expect(onSelect).toHaveBeenCalledWith('thread-older');

    fireEvent.change(screen.getByPlaceholderText('Search threads...'), {
      target: { value: '' },
    });
    expect(screen.getByText('Alpha Chat')).toBeInTheDocument();
    expect(
      screen.queryByText('Older retrieval thread')
    ).not.toBeInTheDocument();
    expect(mockedSearchThreads).toHaveBeenCalledWith(
      expect.objectContaining({
        filters: { workspace_id: 'workspace-1' },
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
  });

  it('does not offer normal-list pagination while a search is active', () => {
    const { rerender } = render(
      <ChatSidebar {...defaultProps} hasMoreThreads />
    );
    fireEvent.change(screen.getByPlaceholderText('Search threads...'), {
      target: { value: 'older retrieval' },
    });

    rerender(<ChatSidebar {...defaultProps} hasMoreThreads />);
    expect(screen.getByDisplayValue('older retrieval')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Show older threads' })
    ).not.toBeInTheDocument();
  });

  it('shows "No messages yet" for empty conversations', () => {
    render(<ChatSidebar {...defaultProps} />);
    const noMsgElements = screen.getAllByText('No messages yet');
    expect(noMsgElements.length).toBeGreaterThan(0);
  });

  it('shows a message-count fallback for persisted threads without a loaded preview', () => {
    render(
      <ChatSidebar
        {...defaultProps}
        conversations={[
          {
            id: 'conv-4',
            title: 'Count Only',
            messages: [],
            updatedAt: Date.now(),
            messageCount: 3,
          },
        ]}
      />
    );

    expect(screen.getByText('3 messages')).toBeInTheDocument();
  });

  it('shows preview text even when full messages are not loaded yet', () => {
    render(
      <ChatSidebar
        {...defaultProps}
        conversations={[
          {
            id: 'conv-4',
            title: 'Preview Only',
            messages: [],
            previewText: 'Persisted preview from thread list',
            updatedAt: Date.now(),
            messageCount: 3,
          },
        ]}
      />
    );

    expect(
      screen.getByText(/Persisted preview from thread/)
    ).toBeInTheDocument();
  });

  it('shows provided message count for active conversations', () => {
    render(
      <ChatSidebar
        {...defaultProps}
        activeId="conv-4"
        conversations={[
          {
            id: 'conv-4',
            title: 'Preview Only',
            messages: [],
            previewText: 'Persisted preview from thread list',
            updatedAt: Date.now(),
            messageCount: 3,
          },
        ]}
      />
    );

    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('truncates long preview text with ellipsis', () => {
    render(<ChatSidebar {...defaultProps} />);
    expect(
      screen.getByText(/This is a longer message that should be truncated.*…$/)
    ).toBeInTheDocument();
  });

  it('truncates a persisted list preview with the same sidebar limit', () => {
    render(
      <ChatSidebar
        {...defaultProps}
        conversations={[
          {
            id: 'conv-preview-long',
            title: 'Long persisted preview',
            messages: [],
            previewText:
              'This persisted transcript preview is intentionally longer than sixty characters so the row stays compact',
            updatedAt: Date.now(),
            messageCount: 2,
          },
        ]}
      />
    );

    expect(
      screen.getByText(
        'This persisted transcript preview is intentionally longer th…'
      )
    ).toBeInTheDocument();
  });

  it('highlights active conversation', () => {
    render(<ChatSidebar {...defaultProps} activeId="conv-1" />);
    const activeRow = screen.getByText('Alpha Chat').closest('button');
    expect(activeRow?.className).toContain('sb-conv');
    expect(activeRow?.className).toMatch(/aurum|ember/);
  });

  it('calls onRename when rename hover action clicked', () => {
    const onRename = vi.fn();
    render(<ChatSidebar {...defaultProps} onRename={onRename} />);
    const row = screen.getByText('Alpha Chat').closest('button')!;
    fireEvent.mouseEnter(row);
    fireEvent.click(screen.getByLabelText('Rename Alpha Chat'));
    expect(onRename).toHaveBeenCalledWith('conv-1');
  });

  it('calls onDelete when delete hover action clicked', () => {
    const onDelete = vi.fn();
    render(<ChatSidebar {...defaultProps} onDelete={onDelete} />);
    const row = screen.getByText('Alpha Chat').closest('button')!;
    fireEvent.mouseEnter(row);
    fireEvent.click(screen.getByLabelText('Delete Alpha Chat'));
    expect(onDelete).toHaveBeenCalledWith('conv-1');
  });

  const clickSelectModeToggle = (): void => {
    const toggle = screen
      .getAllByRole('button')
      .find((button) => button.className.includes('ml-auto'));
    if (!toggle) {
      throw new Error('Select mode toggle not found');
    }
    fireEvent.click(toggle);
  };

  it('enters multi-select mode when Select toolbar button clicked', () => {
    render(<ChatSidebar {...defaultProps} />);
    clickSelectModeToggle();
    expect(screen.getAllByRole('checkbox')).toHaveLength(3);
  });

  it('calls onBulkDelete with selected ids from multi-select mode', () => {
    const onBulkDelete = vi.fn();
    render(<ChatSidebar {...defaultProps} onBulkDelete={onBulkDelete} />);
    clickSelectModeToggle();
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getAllByRole('checkbox')[1]);
    fireEvent.click(screen.getByRole('button', { name: '2' }));
    expect(onBulkDelete).toHaveBeenCalledWith(['conv-1', 'conv-2']);
  });
});

describe('ChatSidebar virtual conversation list', () => {
  const conversations = Array.from({ length: 500 }, (_, index) => ({
    id: `large-${index}`,
    title: `Large conversation ${index}`,
    updatedAt: Date.now(),
    messages: [],
    pinned: index === 0,
  }));

  it('mounts a bounded window while retaining section headings and total size', () => {
    render(
      <ChatSidebar
        conversations={conversations}
        activeId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />
    );
    expect(document.querySelectorAll('.sb-conv').length).toBeLessThan(30);
    expect(screen.getByRole('heading', { name: /Pinned/ })).toBeInTheDocument();
    expect(
      screen.getByRole('list', { name: 'Conversations' })
    ).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')[0]).toHaveAttribute(
      'aria-setsize',
      '500'
    );
    expect(
      screen.queryByText('Large conversation 499')
    ).not.toBeInTheDocument();
  });

  it('reveals a distant active conversation and keeps keyboard focus across virtual windows', async () => {
    const onSelect = vi.fn();
    render(
      <ChatSidebar
        conversations={conversations}
        activeId="large-499"
        onSelect={onSelect}
        onNew={vi.fn()}
      />
    );
    const last = await screen.findByText('Large conversation 499');
    const button = last.closest('button')!;
    button.focus();
    fireEvent.keyDown(button, { key: 'Home' });
    await waitFor(() =>
      expect(
        screen.getByText('Large conversation 0').closest('button')
      ).toHaveFocus()
    );
    fireEvent.keyDown(document.activeElement!, { key: 'End' });
    await waitFor(() =>
      expect(
        screen.getByText('Large conversation 499').closest('button')
      ).toHaveFocus()
    );
    fireEvent.click(document.activeElement!);
    expect(onSelect).toHaveBeenCalledWith('large-499');
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
    await waitFor(() =>
      expect(
        screen.getByText('Large conversation 498').closest('button')
      ).toHaveFocus()
    );
  });

  it('preserves the current scroll window when older conversations are appended', async () => {
    const props = {
      conversations,
      activeId: 'large-0',
      onSelect: vi.fn(),
      onNew: vi.fn(),
    };
    const { rerender } = render(<ChatSidebar {...props} />);
    const first = screen.getByText('Large conversation 0').closest('button')!;
    first.focus();
    fireEvent.keyDown(first, { key: 'End' });
    await waitFor(() =>
      expect(screen.getByText('Large conversation 499')).toBeInTheDocument()
    );
    rerender(
      <ChatSidebar
        {...props}
        conversations={[
          ...conversations,
          {
            id: 'older',
            title: 'Older conversation',
            messages: [],
            updatedAt: 0,
            pinned: false,
          },
        ]}
      />
    );
    expect(screen.getByText('Large conversation 499')).toBeInTheDocument();
    expect(screen.queryByText('Large conversation 0')).not.toBeInTheDocument();
  });

  it('tabs into the next checkbox when selection crosses the mounted window', async () => {
    render(
      <ChatSidebar
        conversations={conversations}
        activeId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />
    );
    fireEvent.click(screen.getByLabelText('Select conversations'));
    const items = screen.getAllByRole('listitem');
    const lastMounted = items.at(-1)!;
    const lastIndex = Number(
      lastMounted.getAttribute('data-conversation-id')!.split('-')[1]
    );
    const button = lastMounted.querySelector('button')!;
    button.focus();
    fireEvent.keyDown(button, { key: 'Tab' });
    await waitFor(() =>
      expect(
        screen.getByLabelText(`Select Large conversation ${lastIndex + 1}`)
      ).toHaveFocus()
    );
  });

  it('retains list focus when scrolling unmounts a focused row', async () => {
    render(
      <ChatSidebar
        conversations={conversations}
        activeId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />
    );
    screen.getByText('Large conversation 0').closest('button')!.focus();
    const list = screen.getByRole('list', { name: 'Conversations' });
    const scroller = list.firstElementChild!;
    Object.defineProperty(scroller, 'clientHeight', { value: 400 });
    Object.defineProperty(scroller, 'scrollHeight', { value: 50000 });
    fireEvent.scroll(scroller, { target: { scrollTop: 30000 } });
    await waitFor(() => expect(list).toHaveFocus());
    fireEvent.keyDown(list, { key: 'ArrowDown' });
    await waitFor(() =>
      expect(
        screen.getByText('Large conversation 1').closest('button')
      ).toHaveFocus()
    );
  });

  it('keeps selection across scrolling and filtering', async () => {
    const onBulkDelete = vi.fn();
    render(
      <ChatSidebar
        conversations={conversations}
        activeId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onBulkDelete={onBulkDelete}
      />
    );
    fireEvent.click(screen.getByLabelText('Select conversations'));
    fireEvent.click(screen.getByLabelText('Select Large conversation 0'));
    const first = screen.getByText('Large conversation 0').closest('button')!;
    first.focus();
    fireEvent.keyDown(first, { key: 'End' });
    await waitFor(() =>
      expect(screen.getByText('Large conversation 499')).toBeInTheDocument()
    );
    fireEvent.click(screen.getByLabelText('Select Large conversation 499'));
    fireEvent.click(screen.getByRole('button', { name: '2' }));
    expect(onBulkDelete).toHaveBeenCalledWith(['large-0', 'large-499']);
  });
});
