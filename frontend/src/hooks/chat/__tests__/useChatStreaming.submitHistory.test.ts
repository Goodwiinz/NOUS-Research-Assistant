// CX2: page renders selectDisplayedMessages(local, store) but handleSubmit
// POSTs from the local array. Store-populated + local-empty (the lazy-load
// window after a thread switch) must still send full history.
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import { agentChatService } from '@/services/agentChatService';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { workspaceService } from '@/services/workspaceService';

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

const route = vi.hoisted(() => ({ params: new URLSearchParams() }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => route.params,
}));

const streamMessageMock = vi.fn();
const listMessagesMock = vi.fn();
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
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: (...args: unknown[]) => listMessagesMock(...args),
  },
}));

const storeBacked2: ChatPageMessage[] = [
  makeChatPageMessage({
    id: 'first-user',
    role: 'user',
    content: 'first turn',
    timestamp: 1,
  }),
  makeChatPageMessage({
    id: 'first-assistant',
    role: 'assistant',
    content: 'first reply',
    timestamp: 2,
  }),
];

function makeParams(overrides: Record<string, unknown> = {}) {
  useChatStore.setState({ currentThreadId: 'thread-A' });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: storeBacked2,
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: 'thread-A',
    setActiveConversationId: vi.fn(),
    activeConversationIdRef: { current: 'thread-A' as string | null },
    dbConversation: null,
    setCurrentThread: vi.fn(),
    enableRAG: false,
    ...overrides,
  };
}

describe('useChatStreaming submit history (CX2)', () => {
  afterEach(() => cleanup());
  beforeEach(() => {
    useChatStore.getState().reset();
    useChatStore.setState({
      isStreaming: false,
      messages: { 'thread-A': [] },
      messagePagination: {
        'thread-A': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 0,
          message_branching: false,
          branch_document_id: null,
          selected_message_id: null,
        },
      },
      messageFreshness: { 'thread-A': 'fresh' },
    });
    useAgentActivityStore.setState({ runs: {} });
    vi.mocked(agentChatService.resumeStream).mockResolvedValue({
      status: 'idle',
    });
    route.params = new URLSearchParams();
    streamMessageMock.mockReset();
    listMessagesMock.mockResolvedValue({ messages: [], has_more: false });
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: { onDone?: (p?: unknown) => void }) => {
        cb.onDone?.({});
        return Promise.resolve();
      }
    );
  });

  it('starts a PDF branch with an explicit null parent', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({
      pdfContext: { documentId: 'doc-1', title: 'Paper' },
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('Question');
    });
    expect(streamMessageMock.mock.calls[0][0]).toMatchObject({
      branch_document_id: 'doc-1',
      parent_message_id: null,
      attachment_ids: ['doc-1'],
      document_only: true,
      page_filter_model: 'cohere',
    });
    expect(streamMessageMock.mock.calls[0][0]).not.toHaveProperty(
      'supersedes_client_message_id'
    );
  });

  it('uses the selected canonical head on a PDF follow-up', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({
      pdfContext: { documentId: 'doc-1', title: 'Paper' },
    });
    useChatStore.setState({
      messagePagination: {
        'thread-A': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 2,
          message_branching: true,
          branch_document_id: 'doc-1',
          selected_message_id: 'canonical-assistant',
        },
      },
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('Follow-up');
    });
    expect(streamMessageMock.mock.calls[0][0].parent_message_id).toBe(
      'canonical-assistant'
    );
  });

  it('edits from the original canonical parent without tombstoning or reusing current range', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({
      pdfContext: {
        documentId: 'doc-1',
        title: 'Paper',
        selectionPageRange: [4, 4],
      },
    });
    const original = {
      id: 'canonical-user',
      thread_id: 'thread-A',
      content: 'old',
      role: 'user' as import('@/types/workspace').MessageRole,
      parent_message_id: 'original-parent',
      created_at: '2026-10-14T00:00:00Z',
      updated_at: '2026-10-14T00:00:00Z',
      client_message_id: 'idempotency-key',
      token_count: 0,
      citations: [],
      attachments: [],
    };
    useChatStore.setState({
      messages: { 'thread-A': [original] },
      messagePagination: {
        'thread-A': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 2,
          message_branching: true,
          branch_document_id: 'doc-1',
          selected_message_id: 'current-head',
        },
      },
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit(
        'Edited',
        [],
        undefined,
        undefined,
        undefined,
        {
          parentMessageId: 'original-parent',
          replacedMessageId: 'canonical-user',
          selectedHeadMessageId: 'current-head',
        }
      );
    });
    expect(streamMessageMock.mock.calls[0][0]).toMatchObject({
      parent_message_id: 'original-parent',
      page_filter_model: 'cohere',
    });
    expect(streamMessageMock.mock.calls[0][0]).not.toHaveProperty(
      'supersedes_client_message_id'
    );
    expect(streamMessageMock.mock.calls[0][0]).not.toHaveProperty(
      'selection_page_range'
    );
  });

  it.each([
    'missing-context',
    'wrong-document',
    'pending-access',
    'selection-in-flight',
    'stale-history',
  ])('fails closed for bound PDF %s', async (condition) => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({
      pdfContext:
        condition === 'missing-context'
          ? null
          : {
              documentId: condition === 'wrong-document' ? 'other' : 'doc-1',
              title: 'Paper',
            },
      pdfSubmissionBlocked: condition === 'pending-access',
    });
    useChatStore.setState({
      messageFreshness: {
        'thread-A': condition === 'stale-history' ? 'stale' : 'fresh',
      },
      messagePagination: {
        'thread-A': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 2,
          message_branching: true,
          branch_document_id: 'doc-1',
          selected_message_id: 'head',
          selectingBranch: condition === 'selection-in-flight',
        },
      },
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('Must stay scoped');
    });
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  it('refuses to convert a nonempty linear chat to PDF branching', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({
      pdfContext: { documentId: 'doc-1', title: 'Paper' },
    });
    useChatStore.setState({
      messages: {
        'thread-A': [
          {
            id: 'existing',
            thread_id: 'thread-A',
            role: 'user' as import('@/types/workspace').MessageRole,
            content: 'Earlier general question',
            created_at: '',
            updated_at: '',
            token_count: 0,
            citations: [],
            attachments: [],
          },
        ],
      },
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('PDF question');
    });
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  it('rejects an edit prepared against a previously selected head', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({
      pdfContext: { documentId: 'doc-1', title: 'Paper' },
    });
    useChatStore.setState({
      messages: {
        'thread-A': [
          {
            id: 'ancestor',
            parent_message_id: null,
            thread_id: 'thread-A',
            role: 'user' as import('@/types/workspace').MessageRole,
            content: 'Ancestor',
            created_at: '',
            updated_at: '',
            token_count: 0,
            citations: [],
            attachments: [],
          },
        ],
      },
      messagePagination: {
        'thread-A': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 2,
          message_branching: true,
          branch_document_id: 'doc-1',
          selected_message_id: 'new-head',
        },
      },
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit(
        'Edit ancestor',
        [],
        undefined,
        undefined,
        undefined,
        {
          parentMessageId: null,
          replacedMessageId: 'ancestor',
          selectedHeadMessageId: 'old-head',
        }
      );
    });
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  it('sends store-backed history when local messages are empty', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('follow-up');
    });

    const payload = streamMessageMock.mock.calls[0][0] as {
      messages: unknown[];
    };
    expect(payload.messages).toHaveLength(3); // 2 history + new turn
    expect(payload).not.toHaveProperty('page_filter_model');
  });

  it('sends PDF-only context with exactly one attachment and the already converted page indices', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () =>
        useChatStreaming(
          makeParams({
            pdfContext: {
              documentId: 'doc-1',
              title: 'Paper',
              selectionPageRange: [0, 2],
            },
          })
        ),
      { wrapper }
    );
    await act(async () => {
      await result.current.handleSubmit('Explain these pages');
    });
    expect(streamMessageMock.mock.calls[0][0]).toMatchObject({
      document_only: true,
      use_rag: true,
      attachment_ids: ['doc-1'],
      selection_page_range: [0, 2],
    });
    expect(streamMessageMock.mock.calls[0][0]).not.toHaveProperty(
      'page_filter_model'
    );
  });

  it('composes a quote once, ranks the PDF without a hard range, and consumes the successful quote', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const pdfContext = {
      documentId: 'doc-1',
      title: 'Paper',
      selectionQuoteRange: [1, 1],
      selectionQuote: '> chosen\n> text',
    };
    const onPdfSelectionConsumed = vi.fn();
    const params = makeParams({ pdfContext, onPdfSelectionConsumed });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('  Why?  ');
    });
    const request = streamMessageMock.mock.calls[0][0];
    expect(request.messages.at(-1).content).toBe(
      '> chosen\n> text\n\n  Why?  '
    );
    expect(request).not.toHaveProperty('selection_page_range');
    expect(request.page_filter_model).toBe('cohere');
    expect(onPdfSelectionConsumed).toHaveBeenCalledExactlyOnceWith(pdfContext);
    expect(result.current.input).toBe('');
  });

  // Mutation-verified: removing useChatStreaming's page_filter_model: 'cohere'
  // request field fails both this case and the quote-only case above.
  // pnpm --dir frontend test src/hooks/chat/__tests__/useChatStreaming.submitHistory.test.ts -t 'ranks the PDF|automatic page ranking'
  it('requests automatic page ranking for a bound PDF without a quote or explicit range', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () =>
        useChatStreaming(
          makeParams({
            pdfContext: { documentId: 'doc-1', title: 'Paper' },
          })
        ),
      { wrapper }
    );
    await act(async () => {
      await result.current.handleSubmit('Explain the paper');
    });
    expect(streamMessageMock.mock.calls[0][0]).toMatchObject({
      document_only: true,
      attachment_ids: ['doc-1'],
      page_filter_model: 'cohere',
    });
    expect(streamMessageMock.mock.calls[0][0]).not.toHaveProperty(
      'selection_page_range'
    );
  });

  it.each(['error', 'throw', 'no acceptance'])(
    'preserves the original question and selection on unaccepted %s',
    async (failure) => {
      const { useChatStreaming } =
        await import('@/hooks/chat/useChatStreaming');
      streamMessageMock.mockImplementation(async (_request, callbacks) => {
        if (failure === 'error') callbacks.onError('Rejected', 'validation');
        if (failure === 'throw') throw new Error('offline');
      });
      const onPdfSelectionConsumed = vi.fn();
      const params = makeParams({
        pdfContext: {
          documentId: 'doc-1',
          title: 'Paper',
          selectionQuote: '> selected',
        },
        onPdfSelectionConsumed,
      });
      const { result } = renderHook(() => useChatStreaming(params), {
        wrapper,
      });
      await act(async () => {
        await result.current.handleSubmit('  original question  ');
      });
      expect(result.current.input).toBe('  original question  ');
      expect(onPdfSelectionConsumed).not.toHaveBeenCalled();
    }
  );

  it('keeps an overlong question intact when the server rejects the composed message', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    streamMessageMock.mockImplementation(async (_request, callbacks) => {
      callbacks.onError('Message too long', 'validation');
    });
    const onPdfSelectionConsumed = vi.fn();
    const params = makeParams({
      pdfContext: {
        documentId: 'doc-1',
        title: 'Paper',
        selectionQuote: '> chosen passage',
      },
      onPdfSelectionConsumed,
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    const question = 'x'.repeat(100_000);
    await act(async () => {
      await result.current.handleSubmit(question);
    });
    expect(streamMessageMock.mock.calls[0][0].messages.at(-1).content).toBe(
      `> chosen passage\n\n${question}`
    );
    expect(result.current.input).toBe(question);
    expect(onPdfSelectionConsumed).not.toHaveBeenCalled();
  });

  it('does not quote an edit or regeneration of already composed content', async () => {
    // Mutation-verified: removing useChatStreaming.ts:2044's !historyOverride
    // condition prepends the newer quote to an already quoted persisted turn.
    // pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.submitHistory.test.ts -t 'does not quote an edit'
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const onPdfSelectionConsumed = vi.fn();
    const params = makeParams({
      pdfContext: {
        documentId: 'doc-1',
        title: 'Paper',
        selectionQuote: '> newer selection',
      },
      onPdfSelectionConsumed,
    });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('> persisted quote\n\nWhy?', []);
    });
    expect(streamMessageMock.mock.calls[0][0].messages.at(-1).content).toBe(
      '> persisted quote\n\nWhy?'
    );
    expect(onPdfSelectionConsumed).not.toHaveBeenCalled();
  });

  it.each(['generation error', 'stopped transport'])(
    'consumes a durably accepted quote after %s',
    async (outcome) => {
      const { useChatStreaming } =
        await import('@/hooks/chat/useChatStreaming');
      streamMessageMock.mockImplementation(async (_request, callbacks) => {
        callbacks.onRunId('durable-run');
        if (outcome === 'generation error')
          callbacks.onError('Generation failed', 'provider');
        // The stopped reader may resolve without a done frame; its accepted
        // user row is still canonical and must not remain an unsent quote.
      });
      const pdfContext = {
        documentId: 'doc-1',
        title: 'Paper',
        selectionPageRange: [1, 1],
        selectionQuote: '> selected',
      };
      const onPdfSelectionConsumed = vi.fn();
      const params = makeParams({ pdfContext, onPdfSelectionConsumed });
      const { result } = renderHook(() => useChatStreaming(params), {
        wrapper,
      });
      await act(async () => {
        await result.current.handleSubmit('Why?');
      });
      expect(onPdfSelectionConsumed).toHaveBeenCalledExactlyOnceWith(
        pdfContext
      );
      expect(result.current.input).toBe('');
    }
  );

  it('does not submit an old PDF quote after its context changes during thread creation', async () => {
    // Mutation-verified: disabling useChatStreaming.ts:2108's PDF context
    // check sends the old quote despite selecting a different PDF meanwhile.
    // pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.submitHistory.test.ts -t 'does not submit an old PDF quote'
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    let resolveThread!: (thread: unknown) => void;
    vi.mocked(workspaceService.createThread).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveThread = resolve;
        })
    );
    const params = makeParams({
      dbConversation: { id: 'conversation', workspace_id: 'workspace' },
      pdfContext: {
        documentId: 'doc-1',
        title: 'Paper',
        selectionQuote: '> old selection',
      },
    });
    useChatStore.setState({ currentThreadId: null });
    const { result, rerender } = renderHook(
      ({ pdfContext }) => useChatStreaming({ ...params, pdfContext }),
      { wrapper, initialProps: { pdfContext: params.pdfContext } }
    );
    let send!: Promise<void>;
    act(() => {
      send = result.current.handleSubmit('  original  ');
    });
    rerender({
      pdfContext: {
        documentId: 'doc-2',
        title: 'Other',
        selectionQuote: '> newer selection',
      },
    });
    await act(async () => {
      resolveThread({
        id: 'created',
        conversation_id: 'conversation',
        title: 'Title',
      });
      await send;
    });
    expect(streamMessageMock).not.toHaveBeenCalled();
    expect(result.current.input).toBe('  original  ');
  });

  it('rejects a conflicting upload instead of silently ignoring it', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () =>
        useChatStreaming(
          makeParams({
            pdfContext: { documentId: 'doc-1', title: 'Paper' },
          })
        ),
      { wrapper }
    );
    await act(async () => {
      await result.current.handleSubmit('Explain', undefined, undefined, [
        'other-doc',
      ]);
    });
    expect(streamMessageMock).not.toHaveBeenCalled();
  });

  // Mutation check: removing the pdfDocumentId guard in handleSubmit makes
  // both cases send an unscoped turn and fails this assertion.
  // Verified by removing that guard, observing failures, restoring, then:
  // pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.submitHistory.test.ts -t 'blocks unresolved or invalid PDF route intent'
  it.each(['', '00000000-0000-4000-8000-000000000001'])(
    'blocks unresolved or invalid PDF route intent %s',
    async (id) => {
      route.params = new URLSearchParams({ pdfDocumentId: id });
      const { useChatStreaming } =
        await import('@/hooks/chat/useChatStreaming');
      const params = makeParams();
      const { result } = renderHook(() => useChatStreaming(params), {
        wrapper,
      });
      await act(async () => {
        await result.current.handleSubmit('Explain the PDF');
      });
      expect(streamMessageMock).not.toHaveBeenCalled();
    }
  );

  it('clears live page-filter diagnostics when a turn ends', async () => {
    const filter = {
      document_id: 'doc-1',
      accepted_pages: [1],
      total_pages: 3,
      short_circuited: true,
      priorities: [],
    };
    let observed: unknown;
    streamMessageMock.mockImplementation((_request, callbacks) => {
      callbacks.onRagContext([], filter);
      observed = useChatStore.getState().streamingPageFilter;
      callbacks.onDone({});
      return Promise.resolve();
    });
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });
    await act(async () => {
      await result.current.handleSubmit('Explain');
    });
    expect(observed).toEqual(filter);
    expect(useChatStore.getState().streamingPageFilter).toBeNull();
  });

  it('uses an explicit truncated history for regeneration', async () => {
    const laterTurn = [
      ...storeBacked2,
      makeChatPageMessage({
        id: 'second-user',
        role: 'user',
        content: 'second turn',
        timestamp: 3,
      }),
      makeChatPageMessage({
        id: 'second-assistant',
        role: 'assistant',
        content: 'second reply',
        timestamp: 4,
      }),
    ];
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams({ displayedMessages: laterTurn });
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('second turn', storeBacked2);
    });

    const payload = streamMessageMock.mock.calls[0][0] as {
      messages: Array<{ content: string }>;
    };
    expect(payload.messages.map((message) => message.content)).toEqual([
      'first turn',
      'first reply',
      'second turn',
    ]);
  });

  it('keeps React-local state limited to the optimistic overlay', async () => {
    streamMessageMock.mockImplementation(
      (
        _request: unknown,
        callbacks: {
          onToken: (content: string) => void;
          onDone: (payload?: unknown) => void;
        }
      ) => {
        callbacks.onToken('follow-up answer');
        callbacks.onDone({ assistant_message_id: 'assistant-2' });
        return Promise.resolve();
      }
    );
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const params = makeParams();
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('follow-up');
    });

    const arrayWrites = params.setMessages.mock.calls
      .map((call) => call[0])
      .filter((value): value is ChatPageMessage[] => Array.isArray(value));
    const finalLocalOverlay = arrayWrites.at(-1)!;
    expect(finalLocalOverlay.map((message) => message.content)).toEqual([
      'follow-up',
      'follow-up answer',
    ]);
  });
});
