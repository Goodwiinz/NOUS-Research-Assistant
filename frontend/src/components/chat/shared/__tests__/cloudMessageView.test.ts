import { describe, expect, it } from 'vitest';
import {
  isThreadSwitchPending,
  mapDbMessageToChatPageMessage,
  mapStoreMessagesToChatMessages,
  selectDisplayedMessages,
  summarizeToolArgs,
  summarizeToolResult,
} from '../cloudMessageView';
import type { ChatMessage } from '@/types/workspace';

describe('cloudMessageView', () => {
  it('maps persisted store messages into chat page message shape', () => {
    const result = mapStoreMessagesToChatMessages([
      {
        id: 'm-1',
        client_message_id: 'runtime-m-1',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [{ document_title: 'Doc 1', score: 0.8 }],
      } as any,
    ]);

    expect(result[0].role).toBe('assistant');
    expect(result[0].content).toBe('Answer');
    expect(result[0].citations?.[0].title).toBe('Doc 1');
    expect(result[0].runtimeId).toBe('runtime-m-1');
    expect(result[0].source).toBe('canonical');
  });

  it('falls back to the persisted id for legacy rows without a client id', () => {
    const message = mapDbMessageToChatPageMessage({
      id: 'legacy-message-id',
      client_message_id: null,
      role: 'user',
      content: 'Legacy question',
      created_at: '2026-03-09T12:00:00Z',
      citations: [],
    } as any);

    expect(message.runtimeId).toBe('legacy-message-id');
    expect(message.source).toBe('canonical');
  });

  it('carries the stopped flag and latency from a persisted message into metadata', () => {
    const [withStop, plain] = mapStoreMessagesToChatMessages([
      {
        id: 'm-1',
        role: 'assistant',
        content: 'Partial',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
        latency_ms: 1500,
        stopped: true,
      } as any,
      {
        id: 'm-2',
        role: 'assistant',
        content: 'Complete',
        created_at: '2026-03-09T12:00:01Z',
        citations: [],
      } as any,
    ]);

    expect(withStop.metadata?.stopped).toBe(true);
    expect(withStop.metadata?.responseTimeMs).toBe(1500);
    expect(plain.metadata).toBeUndefined();
  });

  it('maps persisted plan, plan_reasoning and token usage into the chat page message', () => {
    const [msg] = mapStoreMessagesToChatMessages([
      {
        id: 'm-1',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
        plan: [
          {
            step: 1,
            description: 'Search documents',
            tool: 'search_documents',
            args_hint: { query: 'transformers' },
            depends_on: [],
          },
        ],
        plan_reasoning: 'Search first, then answer from the results.',
        reasoning_summary: 'I compared the strongest retrieved sources.',
        token_usage: { input_tokens: 1200, output_tokens: 340 },
        progress_steps: [
          { phase: 'accepted', detail: 'Request accepted' },
          { phase: 'writing', detail: 'Drafting the response' },
        ],
      } as any,
    ]);

    expect(msg.plan).toHaveLength(1);
    expect(msg.plan?.[0].tool).toBe('search_documents');
    expect(msg.planReasoning).toBe(
      'Search first, then answer from the results.'
    );
    expect(msg.reasoningSummary).toBe(
      'I compared the strongest retrieved sources.'
    );
    expect(msg.metadata?.tokenUsage).toEqual({ input: 1200, output: 340 });
    expect(msg.progressSteps).toEqual([
      { phase: 'accepted', detail: 'Request accepted' },
      { phase: 'writing', detail: 'Drafting the response' },
    ]);
  });

  it('leaves plan, planReasoning and tokenUsage absent for rows persisted without them', () => {
    const [msg] = mapStoreMessagesToChatMessages([
      {
        id: 'm-2',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
      } as any,
    ]);

    expect(msg.plan).toBeUndefined();
    expect(msg.planReasoning).toBeUndefined();
    expect(msg.reasoningSummary).toBeUndefined();
    expect(msg.metadata).toBeUndefined();
  });

  // Regression: the lazy-load path (`useChatSession.mapDbMessageToUiMessage`)
  // previously had a second mapper copy that silently dropped plan +
  // token_usage on thread switch / warm start. It now delegates to
  // mapDbMessageToChatPageMessage, so both paths must agree.
  describe('mapDbMessageToChatPageMessage (canonical single-message mapper)', () => {
    it('carries plan + token_usage so the lazy-load path matches the store path', () => {
      const db = {
        id: 'm-1',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
        latency_ms: 900,
        plan: [
          {
            step: 1,
            description: 'Search',
            tool: 'search_documents',
            args_hint: {},
            depends_on: [],
          },
        ],
        token_usage: { input_tokens: 42, output_tokens: 7 },
      } as any;

      const fromSingle = mapDbMessageToChatPageMessage(db);
      const [fromArray] = mapStoreMessagesToChatMessages([db]);

      expect(fromSingle.plan).toEqual(fromArray.plan);
      expect(fromSingle.metadata?.tokenUsage).toEqual(
        fromArray.metadata?.tokenUsage
      );
      expect(fromSingle.metadata?.tokenUsage).toEqual({
        input: 42,
        output: 7,
      });
      expect(fromSingle.metadata?.responseTimeMs).toBe(900);
      expect(fromSingle).toEqual(fromArray);
    });

    it('carries ttft_ms so a reloaded turn keeps its timing split', () => {
      const db = {
        id: 'm-ttft',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
        latency_ms: 25_800,
        ttft_ms: 24_100,
      } as unknown as ChatMessage;

      const fromSingle = mapDbMessageToChatPageMessage(db);
      const [fromArray] = mapStoreMessagesToChatMessages([db]);

      expect(fromSingle.metadata?.ttftMs).toBe(24_100);
      expect(fromSingle).toEqual(fromArray);
    });

    it('builds metadata from ttft_ms alone', () => {
      // A turn can carry a first-token reading with no latency (the confirm
      // path writes latency_ms=None on one branch). Gating metadata on
      // latency_ms alone would drop the split on those rows.
      const msg = mapDbMessageToChatPageMessage({
        id: 'm-ttft-only',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
        ttft_ms: 1200,
      } as unknown as ChatMessage);

      expect(msg.metadata?.ttftMs).toBe(1200);
      expect(msg.metadata?.responseTimeMs).toBeUndefined();
    });

    it('omits plan/metadata when the row carries no provenance', () => {
      const msg = mapDbMessageToChatPageMessage({
        id: 'm-2',
        role: 'user',
        content: 'hi',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
      } as any);

      expect(msg.plan).toBeUndefined();
      expect(msg.metadata).toBeUndefined();
    });
  });

  it('uses store-backed messages for cloud chat once persisted messages are available', () => {
    const localMessages = [
      {
        role: 'user' as const,
        content: 'optimistic',
        timestamp: 1,
      },
    ];

    const displayed = selectDisplayedMessages({
      localMessages,
      storeMessages: [
        {
          id: 'm-1',
          role: 'user',
          content: 'persisted user',
          created_at: '2026-03-09T12:00:00Z',
          citations: [],
        } as any,
        {
          id: 'm-2',
          role: 'assistant',
          content: 'persisted assistant',
          created_at: '2026-03-09T12:00:01Z',
          citations: [],
        } as any,
      ],
    });

    expect(displayed.map((message) => message.content)).toEqual([
      'persisted user',
      'persisted assistant',
    ]);
  });

  it('keeps local messages for cloud chat until store-backed messages exist', () => {
    const localMessages = [
      {
        role: 'user' as const,
        content: 'optimistic',
        timestamp: 1,
      },
    ];

    expect(
      selectDisplayedMessages({
        localMessages,
        storeMessages: [],
      })
    ).toEqual(localMessages);
  });

  it('keeps local optimistic messages displayed even while a store page is in flight', () => {
    // Regression guard for #1121: the selector must never blanket-hide local
    // messages on a global loading flag — the first send in a new chat has a
    // load in flight for the very thread the local turn belongs to. Stale
    // cross-thread paint is prevented upstream (local messages are cleared /
    // re-adopted per thread on switch), not here.
    const localMessages = [
      {
        role: 'user' as const,
        content: 'just sent',
        timestamp: 1,
      },
    ];

    expect(
      selectDisplayedMessages({
        localMessages,
        storeMessages: [],
      })
    ).toEqual(localMessages);
  });

  it('uses store-backed messages when persisted state is ahead of local cache', () => {
    const displayed = selectDisplayedMessages({
      localMessages: [
        {
          id: 'm-1',
          role: 'user' as const,
          content: 'persisted user',
          timestamp: 1,
        },
      ],
      storeMessages: [
        {
          id: 'm-1',
          role: 'user',
          content: 'persisted user',
          created_at: '2026-03-09T12:00:00Z',
          citations: [],
        } as any,
        {
          id: 'm-2',
          role: 'assistant',
          content: 'persisted assistant',
          created_at: '2026-03-09T12:00:01Z',
          citations: [],
        } as any,
      ],
    });

    expect(displayed.map((message) => message.content)).toEqual([
      'persisted user',
      'persisted assistant',
    ]);
  });
});

describe('mapDbToolExecutions', () => {
  it('returns undefined for absent or empty executions', async () => {
    const { mapDbToolExecutions } = await import('../cloudMessageView');
    expect(mapDbToolExecutions(undefined)).toBeUndefined();
    expect(mapDbToolExecutions([])).toBeUndefined();
  });

  it('maps persisted rows onto activity steps', async () => {
    const { mapDbToolExecutions } = await import('../cloudMessageView');
    const steps = mapDbToolExecutions([
      {
        id: 'te-1',
        tool_name: 'search_arxiv',
        tool_display_name: 'Search arXiv',
        args: { query: 'rag' },
        status: 'completed',
        duration_ms: 1234,
      },
      { tool_name: 'ingest_document', status: 'failed', error: 'boom' },
      { tool_name: 'create_project', status: 'cancelled' },
      { tool_name: 'stale_tool', status: 'running' },
      { tool_name: 'bare_tool' },
    ]);
    expect(steps).toEqual([
      {
        id: 'te-1',
        tool: 'search_arxiv',
        label: 'Search arXiv',
        status: 'done',
        durationMs: 1234,
        argsSummary: 'query: rag',
        args: { query: 'rag' },
      },
      {
        tool: 'ingest_document',
        label: 'Ingest document',
        status: 'error',
        resultSummary: 'boom',
      },
      {
        tool: 'create_project',
        label: 'Create project',
        status: 'cancelled',
      },
      {
        tool: 'stale_tool',
        label: 'Stale Tool',
        status: 'cancelled',
      },
      { tool: 'bare_tool', label: 'Bare Tool', status: 'done' },
    ]);
  });

  it('flows tool_executions through mapStoreMessagesToChatMessages', () => {
    const [msg] = mapStoreMessagesToChatMessages([
      {
        id: 'm-1',
        role: 'assistant',
        content: 'Answer',
        created_at: '2026-03-09T12:00:00Z',
        citations: [],
        tool_executions: [
          { tool_name: 'search_documents', status: 'completed' },
        ],
      } as any,
    ]);
    expect(msg.toolExecutions).toEqual([
      { tool: 'search_documents', label: 'Search documents', status: 'done' },
    ]);
  });
});

describe('summarizeToolArgs', () => {
  it('returns undefined for missing or empty args', () => {
    expect(summarizeToolArgs(undefined)).toBeUndefined();
    expect(summarizeToolArgs({})).toBeUndefined();
  });

  it('joins key/value pairs, skipping null values', () => {
    expect(
      summarizeToolArgs({ query: 'rag', max_results: 5, categories: null })
    ).toBe('query: rag · max_results: 5');
  });

  it('truncates long summaries to one line', () => {
    const long = summarizeToolArgs({ query: 'x'.repeat(300) });
    expect(long!.length).toBeLessThanOrEqual(140);
    expect(long!.endsWith('…')).toBe(true);
  });

  it('collapses multiline arguments into a single activity row', () => {
    expect(summarizeToolArgs({ query: 'first\n\nsecond\tterm' })).toBe(
      'query: first second term'
    );
  });
});

describe('summarizeToolResult', () => {
  it('returns undefined for empty result', () => {
    expect(summarizeToolResult(undefined)).toBeUndefined();
    expect(summarizeToolResult('')).toBeUndefined();
  });

  it('prefers error over message field in JSON payloads', () => {
    expect(
      summarizeToolResult(JSON.stringify({ message: 'ok', error: 'boom' }))
    ).toBe('boom');
    expect(summarizeToolResult(JSON.stringify({ message: 'found 3' }))).toBe(
      'found 3'
    );
  });

  it('falls back to truncated raw text for non-JSON results', () => {
    expect(summarizeToolResult('plain text')).toBe('plain text');
    const long = summarizeToolResult('y'.repeat(300));
    expect(long!.length).toBeLessThanOrEqual(140);
  });

  it('collapses multiline results into a single activity row', () => {
    expect(summarizeToolResult('first\nsecond')).toBe('first second');
  });
});

describe('selectDisplayedMessages local-provenance merge', () => {
  const storeMsg = (id: string, content: string) =>
    ({
      id,
      role: 'assistant',
      content,
      created_at: '2026-03-09T12:00:00Z',
      citations: [],
      latency_ms: 2000,
    }) as any;

  it('keeps plan and tokenUsage when the store swap drops in-memory fields', () => {
    const plan = [
      {
        step: 1,
        description: 'Search arXiv',
        tool: 'search_arxiv',
        args_hint: {},
        depends_on: [],
      },
    ];
    const result = selectDisplayedMessages({
      localMessages: [
        {
          id: 'm-1',
          runtimeId: 'm-1',
          source: 'optimistic',
          role: 'assistant',
          content: 'Answer',
          timestamp: 1,
          plan,
          planReasoning: 'Search arXiv, then answer.',
          metadata: {
            responseTimeMs: 1800,
            tokenUsage: { input: 1200, output: 300 },
            toolsUsed: ['Searching arXiv'],
          },
        },
      ],
      storeMessages: [storeMsg('m-1', 'Answer')],
    });

    expect(result[0].plan).toEqual(plan);
    expect(result[0].planReasoning).toBe('Search arXiv, then answer.');
    expect(result[0].metadata?.tokenUsage).toEqual({
      input: 1200,
      output: 300,
    });
    expect(result[0].metadata?.toolsUsed).toEqual(['Searching arXiv']);
    // Server latency stays canonical over the local estimate.
    expect(result[0].metadata?.responseTimeMs).toBe(2000);
  });

  it('keeps toolExecutions when the store row has none (legacy mode, M3)', () => {
    const toolExecutions = [
      {
        tool: 'search_arxiv',
        label: 'Searching arXiv',
        status: 'done' as const,
      },
    ];
    const result = selectDisplayedMessages({
      localMessages: [
        {
          id: 'm-1',
          runtimeId: 'm-1',
          source: 'optimistic',
          role: 'assistant',
          content: 'Answer',
          timestamp: 1,
          toolExecutions,
        },
      ],
      storeMessages: [storeMsg('m-1', 'Answer')],
    });

    expect(result[0].toolExecutions).toEqual(toolExecutions);
  });

  it('does not merge provenance by role and content when runtime ids differ', () => {
    const result = selectDisplayedMessages({
      localMessages: [
        {
          runtimeId: 'optimistic-answer',
          source: 'optimistic',
          role: 'assistant',
          content: 'Answer',
          timestamp: 1,
          metadata: { tokenUsage: { input: 10, output: 5 } },
        },
      ],
      storeMessages: [storeMsg('m-9', 'Answer')],
      messageFreshness: 'fresh',
    });

    expect(result).toHaveLength(1);
    expect(result[0].runtimeId).toBe('m-9');
    expect(result[0].metadata?.tokenUsage).toBeUndefined();
  });

  it('leaves unmatched store messages untouched', () => {
    const result = selectDisplayedMessages({
      localMessages: [
        {
          id: 'other',
          role: 'assistant',
          content: 'Different turn',
          timestamp: 1,
          plan: [] as never,
          metadata: { tokenUsage: { input: 1, output: 1 } },
        },
      ],
      storeMessages: [storeMsg('m-1', 'Answer')],
    });

    expect(result[0].plan).toBeUndefined();
    expect(result[0].metadata?.tokenUsage).toBeUndefined();
  });
});

describe('selectDisplayedMessages runtime-id overlays', () => {
  const db = (id: string, runtimeId: string, content: string, second: number) =>
    ({
      id,
      client_message_id: runtimeId,
      role: second % 2 === 0 ? 'user' : 'assistant',
      content,
      created_at: `2026-07-15T00:00:${String(second).padStart(2, '0')}Z`,
      citations: [],
    }) as any;

  const local = (
    runtimeId: string,
    role: 'user' | 'assistant',
    content: string,
    timestamp: number,
    source: 'canonical' | 'optimistic' | 'local-only' = 'optimistic'
  ) => ({ runtimeId, source, role, content, timestamp });

  it('retains an optimistic tail when a stale canonical page has equal length but divergent ids', () => {
    const displayed = selectDisplayedMessages({
      localMessages: [
        local('runtime-old-user', 'user', 'Earlier question', 1, 'canonical'),
        local(
          'runtime-old-assistant',
          'assistant',
          'Earlier answer',
          2,
          'canonical'
        ),
        local('runtime-new-user', 'user', 'Find recent arXiv papers', 3),
        local('runtime-new-assistant', 'assistant', 'Five papers', 4),
      ],
      storeMessages: [
        db('old-user', 'runtime-old-user', 'Earlier question', 0),
        db('old-assistant', 'runtime-old-assistant', 'Earlier answer', 1),
        db('stale-user', 'runtime-stale-user', 'Stale question', 2),
        db('stale-assistant', 'runtime-stale-assistant', 'Stale answer', 3),
      ],
      messageFreshness: 'stale',
    });

    expect(displayed.map((message) => message.runtimeId)).toEqual([
      'runtime-old-user',
      'runtime-old-assistant',
      'runtime-stale-user',
      'runtime-stale-assistant',
      'runtime-new-user',
      'runtime-new-assistant',
    ]);
  });

  it('keeps a newer optimistic tail alongside a longer paginated canonical history', () => {
    const storeMessages = Array.from({ length: 100 }, (_, index) =>
      db(`m-${index}`, `runtime-${index}`, `message ${index}`, index)
    );
    const localMessages = [
      ...storeMessages
        .slice(50)
        .map((message: any, index) =>
          local(
            message.client_message_id,
            message.role,
            message.content,
            index,
            'canonical'
          )
        ),
      local('runtime-new-user', 'user', 'new question', 101),
      local('runtime-new-assistant', 'assistant', 'new answer', 102),
    ];

    const displayed = selectDisplayedMessages({
      localMessages,
      storeMessages,
      messageFreshness: 'refreshing',
    });

    expect(displayed).toHaveLength(102);
    expect(displayed.slice(-2).map((message) => message.runtimeId)).toEqual([
      'runtime-new-user',
      'runtime-new-assistant',
    ]);
  });

  it('keeps repeated identical prompts distinct by runtime id', () => {
    const displayed = selectDisplayedMessages({
      localMessages: [
        local('prompt-1', 'user', 'repeat this', 1),
        local('prompt-2', 'user', 'repeat this', 2),
      ],
      storeMessages: [],
      messageFreshness: 'stale',
    });

    expect(displayed.map((message) => message.runtimeId)).toEqual([
      'prompt-1',
      'prompt-2',
    ]);
  });

  it('replaces a matching optimistic runtime id with its canonical row once', () => {
    const displayed = selectDisplayedMessages({
      localMessages: [
        {
          ...local('runtime-1', 'assistant', 'Answer', 1),
          metadata: { tokenUsage: { input: 10, output: 5 } },
        },
      ],
      storeMessages: [db('persisted-1', 'runtime-1', 'Answer', 1)],
      messageFreshness: 'refreshing',
    });

    expect(displayed).toHaveLength(1);
    expect(displayed[0]).toMatchObject({
      id: 'persisted-1',
      runtimeId: 'runtime-1',
      source: 'canonical',
      metadata: { tokenUsage: { input: 10, output: 5 } },
    });
  });

  it('drops unmatched optimistic rows only when canonical freshness is proven', () => {
    const optimistic = local('pending', 'user', 'pending question', 2);
    const error = local(
      'local-error',
      'assistant',
      'Network error',
      3,
      'local-only'
    );
    const storeMessages = [db('persisted-1', 'canonical-1', 'Old answer', 1)];

    expect(
      selectDisplayedMessages({
        localMessages: [optimistic, error],
        storeMessages,
        messageFreshness: 'stale',
      }).map((message) => message.runtimeId)
    ).toEqual(['canonical-1', 'pending', 'local-error']);

    expect(
      selectDisplayedMessages({
        localMessages: [optimistic, error],
        storeMessages,
        messageFreshness: 'fresh',
      }).map((message) => message.runtimeId)
    ).toEqual(['canonical-1', 'local-error']);
  });
});

describe('isThreadSwitchPending', () => {
  const base = {
    activeThreadId: 'thread-B',
    loadingThreadId: 'thread-B',
    localMessageCount: 0,
    storeMessageCount: 0,
  };

  it('is pending while the active thread loads with nothing renderable yet', () => {
    expect(isThreadSwitchPending(base)).toBe(true);
  });

  it('is not pending when local messages exist (first send in a new thread)', () => {
    expect(isThreadSwitchPending({ ...base, localMessageCount: 1 })).toBe(
      false
    );
  });

  it('is not pending when the store already has the thread cached', () => {
    expect(isThreadSwitchPending({ ...base, storeMessageCount: 2 })).toBe(
      false
    );
  });

  it('is not pending when the in-flight load is for another thread', () => {
    expect(
      isThreadSwitchPending({ ...base, loadingThreadId: 'thread-A' })
    ).toBe(false);
  });

  it('is not pending with no load in flight', () => {
    expect(isThreadSwitchPending({ ...base, loadingThreadId: null })).toBe(
      false
    );
  });

  it('is not pending with no active thread', () => {
    expect(
      isThreadSwitchPending({
        ...base,
        activeThreadId: null,
        loadingThreadId: null,
      })
    ).toBe(false);
  });
});
