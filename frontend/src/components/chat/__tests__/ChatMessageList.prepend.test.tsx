import { describe, expect, it, vi, beforeAll } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// The AUI message bubble needs a ThreadRuntime provider that jsdom lacks; the
// scroll-compensation logic under test is independent of bubble internals.
vi.mock('@/components/chat/aui/AuiMessage', () => ({
  AuiMessageByIndex: ({ index }: { index: number }) => (
    <div data-testid={`msg-${index}`} />
  ),
}));
vi.mock('@/components/chat/shared/InlineAgentSummary', () => ({
  InlineAgentSummary: () => null,
}));

import { ChatMessageList } from '@/components/chat/ChatMessageList';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

beforeAll(() => {
  // jsdom lacks these
  Element.prototype.scrollIntoView = vi.fn();
  if (!('ResizeObserver' in globalThis)) {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
    );
  }
});

const msg = (id: string, role: 'user' | 'assistant'): ChatPageMessage =>
  makeChatPageMessage({ id, role, content: `content ${id}`, timestamp: 1 });

const baseProps = {
  activeThreadId: 't-1',
  isLoading: false,
  storeIsStreaming: false,
  storeStreamingContent: '',
  onRegenerate: vi.fn(),
  onCitationClick: vi.fn(),
  hasMore: true,
  isLoadingOlder: false,
  onLoadOlder: vi.fn(),
};

describe('ChatMessageList prepend scroll compensation', () => {
  it('positions a newly hydrated thread at the bottom without smooth traversal', async () => {
    const scrollIntoView = Element.prototype.scrollIntoView as ReturnType<
      typeof vi.fn
    >;
    const { rerender } = render(
      <ChatMessageList
        {...baseProps}
        activeThreadId="t-2"
        messages={[]}
        isLoading
      />
    );
    scrollIntoView.mockClear();

    rerender(
      <ChatMessageList
        {...baseProps}
        activeThreadId="t-2"
        messages={[msg('m1', 'user'), msg('m2', 'assistant')]}
      />
    );

    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    expect(scrollIntoView).toHaveBeenLastCalledWith({ behavior: 'auto' });
    expect(scrollIntoView).not.toHaveBeenCalledWith({ behavior: 'smooth' });
  });

  it('positions a cached thread even when the previous thread was scrolled up', async () => {
    const scrollIntoView = Element.prototype.scrollIntoView as ReturnType<
      typeof vi.fn
    >;
    const { container, rerender } = render(
      <ChatMessageList
        {...baseProps}
        activeThreadId="thread-A"
        messages={[msg('a1', 'user'), msg('a2', 'assistant')]}
      />
    );
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    scrollIntoView.mockClear();

    const scroller = container.querySelector(
      '[aria-label="Conversation transcript"]'
    ) as HTMLElement;
    Object.defineProperty(scroller, 'scrollHeight', {
      configurable: true,
      value: 1200,
    });
    Object.defineProperty(scroller, 'clientHeight', {
      configurable: true,
      value: 300,
    });
    scroller.scrollTop = 50;
    fireEvent.scroll(scroller);
    await waitFor(() =>
      expect(screen.getByText('Jump to latest')).toBeTruthy()
    );

    rerender(
      <ChatMessageList
        {...baseProps}
        activeThreadId="thread-B"
        messages={[msg('b1', 'user'), msg('b2', 'assistant')]}
      />
    );

    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    expect(scrollIntoView).toHaveBeenLastCalledWith({ behavior: 'auto' });
  });

  it('offsets scrollTop by the scrollHeight delta when older messages are prepended', () => {
    const initial = [msg('m3', 'user'), msg('m4', 'assistant')];
    const { container, rerender } = render(
      <ChatMessageList {...baseProps} messages={initial} />
    );

    const scroller = container.querySelector(
      '[aria-label="Conversation transcript"]'
    ) as HTMLElement;
    expect(scroller).toBeTruthy();

    // Simulate layout: current content is 1000px tall, user sits at 100px.
    let scrollHeight = 1000;
    Object.defineProperty(scroller, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });
    scroller.scrollTop = 100;

    // Trigger a rerender so the layout effect records the 1000px baseline
    // (the effect also runs on mount, but scrollHeight was 0 then).
    rerender(<ChatMessageList {...baseProps} messages={[...initial]} />);

    // Prepend two older messages; the DOM "grows" to 1600px.
    scrollHeight = 1600;
    rerender(
      <ChatMessageList
        {...baseProps}
        messages={[msg('m1', 'user'), msg('m2', 'assistant'), ...initial]}
      />
    );

    // 100 + (1600 - 1000) = 700: the messages the user was reading stay put.
    expect(scroller.scrollTop).toBe(700);
  });

  it('does not touch scrollTop on an appended message', () => {
    const initial = [msg('m1', 'user')];
    const { container, rerender } = render(
      <ChatMessageList {...baseProps} messages={initial} />
    );
    const scroller = container.querySelector(
      '[aria-label="Conversation transcript"]'
    ) as HTMLElement;
    let scrollHeight = 500;
    Object.defineProperty(scroller, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });
    scroller.scrollTop = 100;
    rerender(<ChatMessageList {...baseProps} messages={[...initial]} />);

    scrollHeight = 800;
    rerender(
      <ChatMessageList
        {...baseProps}
        messages={[...initial, msg('m2', 'assistant')]}
      />
    );
    expect(scroller.scrollTop).toBe(100); // append → auto-scroll handles it, not us
  });
  it('does not call prepended older history "New messages"', async () => {
    const initial = [msg('p3', 'user'), msg('p4', 'assistant')];
    const { container, rerender } = render(
      <ChatMessageList {...baseProps} activeThreadId="t-p" messages={initial} />
    );
    const scroller = container.querySelector(
      '[aria-label="Conversation transcript"]'
    ) as HTMLElement;
    Object.defineProperty(scroller, 'scrollHeight', {
      configurable: true,
      value: 1200,
    });
    Object.defineProperty(scroller, 'clientHeight', {
      configurable: true,
      value: 300,
    });
    scroller.scrollTop = 400;
    fireEvent.scroll(scroller);
    await waitFor(() =>
      expect(screen.getByText('Jump to latest')).toBeTruthy()
    );

    rerender(
      <ChatMessageList
        {...baseProps}
        activeThreadId="t-p"
        messages={[msg('p1', 'user'), msg('p2', 'assistant'), ...initial]}
      />
    );

    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.queryByText('New messages')).toBeNull();
    expect(screen.getByText('Jump to latest')).toBeTruthy();
  });
});
