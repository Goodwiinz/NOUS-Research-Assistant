/**
 * Once the user scrolls up, the auto-scroll effect bails out entirely — which
 * also swallowed the user's OWN send: the new bubble stacked below the fold
 * and the send looked like it did nothing. The user's message must always pull
 * the view back down; content they did not initiate must not.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';

import { ChatMessageList } from '../ChatMessageList';
import { ChatRuntimeProvider } from '../aui/ChatRuntimeProvider';
import type { ChatPageMessage } from '../shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

vi.mock('../shared/ChatBubble', () => ({
  ChatBubble: ({ message }: { message: ChatPageMessage }): ReactElement => (
    <div>{message.content}</div>
  ),
}));
vi.mock('../shared/InlineAgentSummary', () => ({
  InlineAgentSummary: () => null,
}));
vi.mock('../CommandOutputBubble', () => ({ CommandOutputBubble: () => null }));
vi.mock('@/store/chat-store', () => ({
  useChatStore: (selector: (state: { streamingCitations: [] }) => unknown) =>
    selector({ streamingCitations: [] }),
}));

const scrollIntoView = vi.fn();

const baseProps = {
  isLoading: false,
  storeIsStreaming: false,
  storeStreamingContent: '',
  onRegenerate: () => {},
  onCitationClick: () => {},
};

const history: ChatPageMessage[] = [
  makeChatPageMessage({ role: 'user', content: 'first', timestamp: 1 }),
  makeChatPageMessage({ role: 'assistant', content: 'answer', timestamp: 2 }),
];

function tree(messages: ChatPageMessage[]): ReactElement {
  return (
    <ChatRuntimeProvider
      messages={messages}
      isRunning={false}
      onSend={() => {}}
      onCancel={() => {}}
    >
      <ChatMessageList
        {...baseProps}
        activeThreadId="thread-A"
        messages={messages}
      />
    </ChatRuntimeProvider>
  );
}

/** Put the transcript in the "user scrolled up" state the guard reacts to.
 * Queries are scoped to this render's own container — a global `screen` lookup
 * picks up DOM left behind by other suites when files share an environment. */
async function scrollAway(view: ReturnType<typeof render>): Promise<void> {
  const container = view.getByRole('region', {
    name: 'Conversation transcript',
  });
  Object.defineProperty(container, 'scrollHeight', {
    value: 2000,
    configurable: true,
  });
  Object.defineProperty(container, 'clientHeight', {
    value: 400,
    configurable: true,
  });
  container.scrollTop = 0;
  fireEvent.scroll(container);
  await waitFor(() =>
    expect(within(view.container).getByText('New messages')).toBeTruthy()
  );
}

describe('ChatMessageList auto-scroll while scrolled away', () => {
  beforeEach(() => {
    scrollIntoView.mockClear();
    Element.prototype.scrollIntoView = scrollIntoView;
  });

  it('scrolls to a message the user just sent and clears the jump button', async () => {
    const view = render(tree(history));
    const { rerender } = view;
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    await scrollAway(view);
    scrollIntoView.mockClear();

    rerender(
      tree([
        ...history,
        makeChatPageMessage({ role: 'user', content: 'second', timestamp: 3 }),
      ])
    );

    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    // The scroll happens in rAF, but AnimatePresence keeps the jump button
    // mounted until its exit animation completes. Wait for that visible
    // outcome separately instead of treating a scroll call as DOM removal.
    await waitFor(() =>
      expect(within(view.container).queryByText('New messages')).toBeNull()
    );
  });

  it('still respects the guard for content the user did not initiate', async () => {
    const view = render(tree(history));
    const { rerender } = view;
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    await scrollAway(view);
    scrollIntoView.mockClear();

    rerender(
      tree([
        ...history,
        makeChatPageMessage({
          role: 'assistant',
          content: 'background answer',
          timestamp: 3,
        }),
      ])
    );

    // Give the effect's animation frame a chance to run before asserting the
    // negative.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(within(view.container).getByText('New messages')).toBeTruthy();
  });
});
