import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ChatMessageList } from '../ChatMessageList';
import { ChatRuntimeProvider } from '../aui/ChatRuntimeProvider';
import type { ChatPageMessage } from '../shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

vi.mock('../shared/ChatBubble', () => ({
  ChatBubble: ({ message }: any) => <div>{message.content}</div>,
}));
vi.mock('../shared/InlineAgentSummary', () => ({
  InlineAgentSummary: () => null,
}));
vi.mock('../CommandOutputBubble', () => ({ CommandOutputBubble: () => null }));
vi.mock('@/store/chat-store', () => ({
  useChatStore: (selector: any) => selector({ streamingCitations: [] }),
}));

const baseProps = {
  isLoading: false,
  storeIsStreaming: false,
  storeStreamingContent: '',
  onRegenerate: () => {},
  onCitationClick: () => {},
};

function msgs(content: string): ChatPageMessage[] {
  return [
    makeChatPageMessage({ role: 'user', content: 'q', timestamp: 1 }),
    makeChatPageMessage({ role: 'assistant', content, timestamp: 2 }),
  ];
}

function tree(threadId: string, messages: ChatPageMessage[]) {
  return (
    <ChatRuntimeProvider
      messages={messages}
      isRunning={false}
      onSend={() => {}}
      onCancel={() => {}}
    >
      <ChatMessageList
        {...baseProps}
        activeThreadId={threadId}
        messages={messages}
      />
    </ChatRuntimeProvider>
  );
}

describe('ChatMessageList thread-switch remount', () => {
  it('remounts the message-row subtree when activeThreadId changes (keyed by thread)', () => {
    // Same content in both threads so any DOM reuse would be from RECONCILIATION,
    // not a content diff. A thread-keyed subtree must instead UNMOUNT the old
    // node and mount a fresh one — this is what prevents React from reconciling
    // index-addressed MessageByIndex fibers across threads (the crash path).
    const messages = msgs('CARRYOVER');
    const { rerender } = render(tree('thread-A', messages));

    const nodeA = screen.getByText('CARRYOVER');
    expect(document.body.contains(nodeA)).toBe(true);

    rerender(tree('thread-B', messages));

    // Keyed remount: the original row node is gone, a fresh one is mounted.
    expect(document.body.contains(nodeA)).toBe(false);
    expect(screen.getByText('CARRYOVER')).not.toBe(nodeA);
  });

  it('keeps the same key (no remount) when the thread id is unchanged', () => {
    // The key is derived only from the thread id, so a re-render on the SAME
    // thread must not change it (else appends/streaming would remount + flicker).
    const messages = msgs('STABLE');
    const { rerender } = render(tree('thread-A', messages));
    const node = screen.getByText('STABLE');

    rerender(tree('thread-A', messages));

    expect(document.body.contains(node)).toBe(true);
    expect(screen.getByText('STABLE')).toBe(node);
  });

  it('replaces every row when the same thread receives a same-count canonical page', () => {
    const optimistic = msgs('OPTIMISTIC ANSWER');
    const canonical = msgs('CANONICAL ANSWER');
    const { rerender } = render(tree('thread-A', optimistic));

    expect(screen.getByText('OPTIMISTIC ANSWER')).toBeInTheDocument();
    rerender(tree('thread-A', canonical));

    expect(screen.queryByText('OPTIMISTIC ANSWER')).not.toBeInTheDocument();
    expect(screen.getByText('CANONICAL ANSWER')).toBeInTheDocument();
  });

  it('exposes stable runtime and persisted identities for browser reconciliation checks', () => {
    const message = makeChatPageMessage({
      id: 'db-answer',
      runtimeId: 'runtime-answer',
      source: 'canonical',
      role: 'assistant',
      content: 'IDENTIFIED',
      timestamp: 2,
    });
    render(tree('thread-A', [message]));

    expect(
      document.querySelector('[data-runtime-id="runtime-answer"]')
    ).toHaveAttribute('data-persisted-id', 'db-answer');
  });
});
