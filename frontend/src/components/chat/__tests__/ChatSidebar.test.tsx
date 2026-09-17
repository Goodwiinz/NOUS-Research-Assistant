import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ChatSidebar } from '../ChatSidebar';

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

  it('keeps older-thread pagination reachable while a search is active', () => {
    const onLoadMoreThreads = vi.fn();
    render(
      <ChatSidebar
        {...defaultProps}
        hasMoreThreads
        onLoadMoreThreads={onLoadMoreThreads}
      />
    );
    fireEvent.change(screen.getByPlaceholderText('Search threads...'), {
      target: { value: 'older' },
    });

    expect(
      screen.getByText(
        'Search covers loaded threads. Load older threads to search more.'
      )
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show older threads' }));
    expect(onLoadMoreThreads).toHaveBeenCalledTimes(1);
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

  const clickSelectModeToggle = () => {
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
