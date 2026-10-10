/**
 * Live message timing starts with the turn and remains mounted while answer
 * tokens arrive; the server heartbeat only corrects the local clock.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, type RenderResult } from '@testing-library/react';

import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';
import { AuiMessageByIndex, formatStreamingElapsed } from '../AuiMessage';

vi.mock('@/components/chat/shared/InlineAgentSummary', () => ({
  InlineAgentSummary: () => null,
}));

function renderStreamingTurn(): RenderResult {
  const messages = [
    makeChatPageMessage({
      id: 'u1',
      role: 'user',
      content: 'go',
      timestamp: 1,
    }),
    makeChatPageMessage({
      runtimeId: 'a1',
      role: 'assistant',
      content: '',
      timestamp: 2,
      isStreaming: true,
    }),
  ];
  return render(
    <ChatRuntimeProvider
      messages={messages}
      isRunning
      onSend={() => {}}
      onCancel={() => {}}
    >
      <AuiMessageByIndex index={1} message={messages[1]} />
    </ChatRuntimeProvider>
  );
}

describe('streaming reasoning panel and elapsed time', () => {
  beforeEach(() => {
    useChatStore.setState({
      streamingContent: '',
      streamingSteps: [],
      streamingProgress: [],
      streamingPlan: [],
      streamingPlanReasoning: '',
      streamingReasoning: '',
      streamingCitations: [],
      isRetrievingRag: false,
      streamingElapsedMs: null,
      streamingPhase: 'accepted',
      streamingStatusDetail: null,
    });
  });

  afterEach(() => vi.useRealTimers());

  it('renders message timing immediately and updates it during streaming', () => {
    vi.useFakeTimers();
    renderStreamingTurn();

    expect(
      document.querySelector('[data-slot="reasoning-panel"]')
    ).toHaveTextContent('Starting');
    expect(screen.getByText('0.0s')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(9_400));
    expect(screen.getByText('9.4s')).toBeInTheDocument();
  });

  it('renders the server-reported phase instead of guessing from the RAG toggle', () => {
    useChatStore.setState({
      isRetrievingRag: true,
      streamingPhase: 'routing',
    });
    renderStreamingTurn();

    expect(
      document.querySelector('[data-slot="reasoning-panel"]')
    ).toHaveTextContent('Choosing approach');
    expect(screen.queryByText('Reading sources')).not.toBeInTheDocument();
  });

  it('prefers the specific live activity over the coarse phase', () => {
    useChatStore.setState({
      streamingPhase: 'routing',
      streamingStatusDetail: 'Choosing the safest response path',
    });
    renderStreamingTurn();

    expect(
      document.querySelector('[data-slot="reasoning-panel"]')
    ).toHaveTextContent('Choosing the safest response path');
    expect(screen.queryByText('Choosing approach')).not.toBeInTheDocument();
  });

  it('shows every explicit server progress step', () => {
    useChatStore.setState({
      streamingProgress: [
        { phase: 'accepted', detail: 'Request accepted' },
        { phase: 'retrieving', detail: 'Reading relevant sources' },
        { phase: 'writing', detail: 'Drafting the response' },
      ],
    });
    renderStreamingTurn();

    const panel = document.querySelector('[data-slot="reasoning-panel"]');
    expect(panel).toHaveTextContent('Request accepted');
    expect(panel).toHaveTextContent('Reading relevant sources');
    expect(panel).toHaveTextContent('Drafting the response');
  });

  it('shows provider summarized reasoning separately from progress', () => {
    useChatStore.setState({
      streamingProgress: [
        { phase: 'retrieving', detail: 'Reading relevant sources' },
      ],
      streamingReasoning: 'Comparing the strongest evidence across sources.',
    });
    renderStreamingTurn();

    const panel = document.querySelector('[data-slot="reasoning-panel"]');
    expect(panel).toHaveTextContent('Reading relevant sources');
    expect(panel).toHaveTextContent('Reasoning summary');
    expect(panel).toHaveTextContent(
      'Comparing the strongest evidence across sources.'
    );
  });

  it('shows planner rationale even when the planner emitted no steps', () => {
    useChatStore.setState({
      streamingPlanReasoning: 'The request needs a direct evidence check.',
    });
    renderStreamingTurn();

    expect(screen.getByText('Planner rationale')).toBeInTheDocument();
    expect(
      screen.getByText('The request needs a direct evidence check.')
    ).toBeInTheDocument();
  });

  it('keeps message timing visible after answer tokens arrive', () => {
    useChatStore.setState({ streamingElapsedMs: 47_000 });
    useChatStore.setState({ streamingContent: 'Drafting the answer' });
    renderStreamingTurn();

    const timing = document.querySelector('[data-slot="message-timing"]');
    expect(
      document.querySelector('[data-slot="reasoning-panel"]')
    ).toBeTruthy();
    expect(timing).toHaveTextContent('total47.0s');
    expect(timing).toHaveAttribute('aria-live', 'off');
  });

  it('renders retrieved passages from live RAG context', () => {
    useChatStore.setState({
      streamingCitations: [
        {
          document_id: 'doc-1',
          title: 'Attention Is All You Need',
          content: 'The Transformer uses self-attention instead of recurrence.',
          score: 0.91,
        },
      ],
      isRetrievingRag: false,
    });
    renderStreamingTurn();

    const retrieval = document.querySelector('[data-slot="retrieval-chunks"]');
    expect(retrieval).toHaveTextContent('1 passage above threshold');
    expect(retrieval).toHaveTextContent('Attention Is All You Need');
    expect(retrieval).toHaveTextContent('0.91');
    expect(retrieval?.querySelector('[style]')).toHaveStyle({
      transform: 'scaleX(0.91)',
    });
  });

  it('only displays retrieval scores that are finite numbers', () => {
    useChatStore.setState({
      streamingCitations: [
        {
          document_id: 'doc-valid',
          title: 'Valid score',
          content: 'A valid passage.',
          score: 0.91,
        },
        {
          document_id: 'doc-string',
          title: 'String score',
          content: 'An injected string score.',
          score: '0.87',
        },
        {
          document_id: 'doc-null',
          title: 'Null score',
          content: 'A null score.',
          score: null,
        },
        {
          document_id: 'doc-nan',
          title: 'NaN score',
          content: 'A non-finite score.',
          score: Number.NaN,
        },
        {
          document_id: 'doc-infinity',
          title: 'Infinite score',
          content: 'Another non-finite score.',
          score: Number.POSITIVE_INFINITY,
        },
      ],
    });

    renderStreamingTurn();

    const retrieval = document.querySelector('[data-slot="retrieval-chunks"]');
    expect(retrieval).toHaveTextContent('1 passage above threshold');
    expect(retrieval).toHaveTextContent('0.91');
    expect(retrieval).not.toHaveTextContent('0.87');
    expect(retrieval).not.toHaveTextContent('0.00');
    expect(retrieval).not.toHaveTextContent('NaN');
    expect(retrieval).not.toHaveTextContent('Infinity');
  });

  it('keeps per-tick step and retrieval lists out of live regions', () => {
    useChatStore.setState({
      streamingProgress: [
        { phase: 'accepted', detail: 'Request accepted' },
        { phase: 'retrieving', detail: 'Reading relevant sources' },
      ],
      streamingCitations: [
        {
          document_id: 'doc-1',
          title: 'Live source',
          content: 'A growing passage.',
          score: 0.91,
        },
      ],
    });

    renderStreamingTurn();

    const reasoning = document.querySelector('[data-slot="reasoning-panel"]');
    const retrieval = document.querySelector('[data-slot="retrieval-chunks"]');
    expect(reasoning).not.toHaveAttribute('role', 'status');
    expect(reasoning).toHaveAttribute('aria-live', 'off');
    expect(retrieval).not.toHaveAttribute('role', 'status');
    expect(retrieval).toHaveAttribute('aria-live', 'off');
  });

  it('formats elapsed readings', () => {
    expect(formatStreamingElapsed(null)).toBeNull();
    expect(formatStreamingElapsed(400)).toBeNull();
    expect(formatStreamingElapsed(15_400)).toBe('15s');
    expect(formatStreamingElapsed(62_000)).toBe('1m 02s');
    expect(formatStreamingElapsed(3_725_000)).toBe('62m 05s');
  });
});
