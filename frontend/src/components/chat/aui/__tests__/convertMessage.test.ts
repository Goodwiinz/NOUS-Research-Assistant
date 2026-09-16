import { describe, expect, it } from 'vitest';

import type {
  ActivityStep,
  ChatPageMessage,
} from '@/components/chat/shared/cloudMessageView';
import { convertMessage, toToolCallParts } from '../convertMessage';

function makeMessage(
  overrides: Partial<ChatPageMessage> = {}
): ChatPageMessage {
  return {
    id: 'm1',
    runtimeId: 'runtime-m1',
    source: 'canonical',
    role: 'assistant',
    content: 'Hello there',
    timestamp: 1720000000000,
    ...overrides,
  };
}

describe('convertMessage', () => {
  it('preserves running, cancelled, error, and completed status for both clients', () => {
    expect(convertMessage(makeMessage({ isStreaming: true })).status).toEqual({
      type: 'running',
    });
    expect(
      convertMessage(
        makeMessage({ isStreaming: true, metadata: { stopped: true } })
      ).status
    ).toEqual({ type: 'incomplete', reason: 'cancelled' });
    expect(
      convertMessage(makeMessage({ error: { message: 'Stream failed' } }))
        .status
    ).toEqual({ type: 'incomplete', reason: 'error', error: 'Stream failed' });
    expect(convertMessage(makeMessage({ content: '' })).status).toEqual({
      type: 'complete',
      reason: 'stop',
    });
  });

  it('converts a plain assistant message to a single text part', () => {
    const result = convertMessage(makeMessage());

    expect(result.id).toBe('runtime-m1');
    expect(result.role).toBe('assistant');
    expect(result.createdAt).toEqual(new Date(1720000000000));
    expect(result.content).toEqual([{ type: 'text', text: 'Hello there' }]);
  });

  it('omits createdAt when the message has no timestamp (optimistic local insert)', () => {
    const result = convertMessage(makeMessage({ timestamp: 0 }));

    expect(result.createdAt).toBeUndefined();
    expect(result.content).toEqual([{ type: 'text', text: 'Hello there' }]);
  });

  it('maps toolExecutions to tool-call parts before the text part', () => {
    const steps: ActivityStep[] = [
      {
        tool: 'search_documents',
        label: 'Searching documents',
        status: 'done',
        argsSummary: 'query: transformers',
        resultSummary: 'Found 3 documents',
      },
      {
        tool: 'ingest_arxiv',
        label: 'Ingesting paper',
        status: 'error',
        argsSummary: 'id: 2401.00001',
        resultSummary: 'Timed out',
      },
    ];
    const result = convertMessage(makeMessage({ toolExecutions: steps }));
    const content = result.content as Array<Record<string, unknown>>;

    expect(content).toHaveLength(3);
    expect(content[0]).toEqual({
      type: 'tool-call',
      toolCallId: 'runtime-m1-tool-0',
      toolName: 'search_documents',
      args: {},
      argsText: 'query: transformers',
      result: 'Found 3 documents',
    });
    expect(content[1]).toEqual({
      type: 'tool-call',
      toolCallId: 'runtime-m1-tool-1',
      toolName: 'ingest_arxiv',
      args: {},
      argsText: 'id: 2401.00001',
      result: 'Timed out',
      isError: true,
    });
    expect(content[2]).toEqual({ type: 'text', text: 'Hello there' });
  });

  it('emits no result and no isError for a running step', () => {
    const steps: ActivityStep[] = [
      { tool: 'search_arxiv', label: 'Searching arXiv', status: 'running' },
    ];
    const [part] = toToolCallParts('m1', steps);

    expect(part).not.toHaveProperty('result');
    expect(part).not.toHaveProperty('isError');
  });

  it('defaults args to {} and argsText to empty string when argsSummary is missing', () => {
    const steps: ActivityStep[] = [
      { tool: 'list_projects', label: 'Listing projects', status: 'done' },
    ];
    const [part] = toToolCallParts('m1', steps);

    expect(part.args).toEqual({});
    expect(part.argsText).toBe('');
  });

  it('emits an approval tool-call part for a pendingApproval message', () => {
    const msg: ChatPageMessage = {
      runtimeId: 'approval-runtime-id',
      source: 'local-only',
      role: 'assistant',
      content: '',
      timestamp: 1,
      pendingApproval: {
        id: 'approval-1',
        tools: [
          {
            name: 'ingest_arxiv_papers',
            args: { paper_ids: ['2605.1'] },
          },
        ],
      },
    };
    const converted = convertMessage(msg);
    const parts = converted.content as Array<{
      type: string;
      toolName?: string;
      args?: Record<string, unknown>;
    }>;
    const approval = parts.find((p) => p.type === 'tool-call') as {
      toolName?: string;
      args?: Record<string, unknown>;
      approval?: { id: string; approved?: boolean };
    };
    expect(approval?.toolName).toBe('__nous_approval__');
    expect(approval?.args).toEqual({
      tools: [
        {
          name: 'ingest_arxiv_papers',
          args: { paper_ids: ['2605.1'] },
        },
      ],
    });
    // Real approval gate (approved omitted = pending) so respondToApproval is
    // callable and routes to the adapter's onRespondToToolApproval.
    expect(approval?.approval?.id).toBeTruthy();
    expect(approval?.approval?.approved).toBeUndefined();
  });

  it('passes structured args through to the tool-call part', () => {
    const steps: ActivityStep[] = [
      {
        tool: 'search_documents',
        label: 'Searching',
        status: 'done',
        argsSummary: 'query: rag',
        args: { query: 'rag', limit: 5 },
      },
    ];
    const [part] = toToolCallParts('m1', steps);

    expect(part.args).toEqual({ query: 'rag', limit: 5 });
    expect(part.argsText).toBe('query: rag');
  });

  it('keeps toolCallIds stable when a persisted id arrives later', () => {
    const steps: ActivityStep[] = [
      { tool: 'search_documents', label: 'Searching', status: 'running' },
    ];
    const result = convertMessage(
      makeMessage({ id: undefined, toolExecutions: steps })
    );
    const content = result.content as Array<Record<string, unknown>>;

    expect(content[0].toolCallId).toBe('runtime-m1-tool-0');
  });

  it('returns referentially stable parts for the same steps array reference', () => {
    const steps: ActivityStep[] = [
      { tool: 'search_documents', label: 'Searching', status: 'running' },
    ];

    expect(toToolCallParts('m1', steps)).toBe(toToolCallParts('m1', steps));
  });

  it('does not reuse cached tool ids across different messages', () => {
    const steps: ActivityStep[] = [
      { tool: 'search_documents', label: 'Searching', status: 'done' },
    ];

    expect(toToolCallParts('m1', steps)[0].toolCallId).toBe('m1-tool-0');
    expect(toToolCallParts('m2', steps)[0].toolCallId).toBe('m2-tool-0');
  });

  it('passes invocation ids and structured results to registered tool UIs', () => {
    const result = { total: 2, documents: [{ id: 'd1' }, { id: 'd2' }] };
    const [part] = toToolCallParts('m1', [
      {
        id: 'call-7',
        tool: 'search_documents',
        label: 'Search documents',
        status: 'done',
        result,
      },
    ]);

    expect(part.toolCallId).toBe('m1-tool-call-7');
    expect(part.result).toBe(result);
  });
});
