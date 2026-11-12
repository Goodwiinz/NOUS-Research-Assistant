/**
 * Round-3 audit regressions for generation lifecycle bookkeeping in the agent
 * chat store: H5 (duplicate placeholder on SSE→durable fallback), H6 (a
 * confirmation superseding a live turn strands its bubble), M3 (poll-budget
 * exhaustion), M6 (clearMessages leaves the generation running), M7 (orphaned
 * tool spinners / stale plan) and M13 (SSE confirmation routed to the legacy
 * job endpoint).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAgentChatStore } from '@/store/agentChatStore';
import type { AgentStreamCallbacks } from '@/services/agentChatService';

const serviceMocks = vi.hoisted(() => ({
  listThreads: vi.fn(async () => ({ threads: [] })),
  getThreadMessages: vi.fn(async () => ({ messages: [] })),
  streamMessage: vi.fn(async () => {}),
  startDurableRun: vi.fn(async () => ({ runId: 'run-stub' })),
  getDurableRunStatus: vi.fn(async () => ({ status: 'PENDING' })),
  streamConfirm: vi.fn(async () => {}),
  confirmAction: vi.fn(async () => {}),
  completeDurableConfirmation: vi.fn(async () => {}),
  pollJob: vi.fn(async () => ({ status: 'pending' })),
}));

vi.mock('@/services/agentChatService', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/services/agentChatService')>();
  return {
    isTerminalJobStatus: actual.isTerminalJobStatus,
    agentChatService: serviceMocks,
  };
});

function getAbortController(): AbortController | null {
  return (
    useAgentChatStore.getState() as unknown as {
      _abortController: AbortController | null;
    }
  )._abortController;
}

describe('agentChatStore generation lifecycle', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    serviceMocks.getThreadMessages.mockResolvedValue({ messages: [] });
    serviceMocks.startDurableRun.mockResolvedValue({ runId: 'run-stub' });
    serviceMocks.getDurableRunStatus.mockResolvedValue({ status: 'PENDING' });
  });

  it('keeps one settled bubble and no durable re-run when SSE throws mid-stream (H5, A4-AG3)', async () => {
    serviceMocks.streamMessage.mockImplementation((async (
      _req: unknown,
      callbacks: AgentStreamCallbacks
    ) => {
      callbacks.onToken?.('partial answer');
      throw new Error('socket hang up');
    }) as never);

    useAgentChatStore.getState().setInputValue('hello');
    await useAgentChatStore.getState().sendMessage();

    // A streamed token means the backend accepted the run; re-running the
    // turn through the durable path would execute it twice (A4-AG3).
    expect(serviceMocks.startDurableRun).not.toHaveBeenCalled();
    expect(useAgentChatStore.getState().isStreaming).toBe(false);

    const assistants = useAgentChatStore
      .getState()
      .messages.filter((m) => m.role === 'assistant');
    expect(assistants).toHaveLength(1);
    expect(new Set(assistants.map((m) => m.id)).size).toBe(1);
  });

  it('settles the superseded turn when a confirmation is approved (H6)', async () => {
    const supersededController = new AbortController();
    useAgentChatStore.setState({
      activeThreadId: 'thread-A',
      messages: [
        {
          id: 'a-live',
          role: 'assistant',
          content: '',
          timestamp: new Date(),
          isStreaming: true,
        },
        {
          id: 'a-1',
          role: 'assistant',
          content: 'Waiting for your confirmation...',
          timestamp: new Date(),
        },
      ],
      pendingConfirmations: {
        'thread-A': {
          threadId: 'thread-A',
          assistantMessageId: 'a-1',
          jobId: 'thread-A',
          origin: 'sse',
          tools: [{ name: 'ingest_arxiv', args: {} }],
          message: 'Confirm?',
        },
      },
    });
    (
      useAgentChatStore.getState() as unknown as {
        _abortController: AbortController | null;
      }
    )._abortController = supersededController;

    await useAgentChatStore.getState().confirmAction('thread-A', true);

    expect(supersededController.signal.aborted).toBe(true);
    expect(
      useAgentChatStore.getState().messages.find((m) => m.id === 'a-live')
    ).toBeUndefined();
  });

  it('fails an SSE confirmation fast instead of calling the legacy job endpoint (M13)', async () => {
    serviceMocks.streamConfirm.mockRejectedValueOnce(new Error('sse down'));
    const pending = {
      threadId: 'thread-A',
      assistantMessageId: 'a-1',
      jobId: 'thread-A',
      origin: 'sse' as const,
      tools: [{ name: 'ingest_arxiv', args: {} }],
      message: 'Confirm?',
    };
    useAgentChatStore.setState({
      activeThreadId: 'thread-A',
      messages: [
        {
          id: 'a-1',
          role: 'assistant',
          content: 'Waiting for your confirmation...',
          timestamp: new Date(),
        },
      ],
      pendingConfirmations: { 'thread-A': pending },
    });

    await useAgentChatStore.getState().confirmAction('thread-A', true);

    expect(serviceMocks.confirmAction).not.toHaveBeenCalled();
    // Card restored, so Approve stays retryable.
    expect(
      useAgentChatStore.getState().pendingConfirmations['thread-A']
    ).toMatchObject({ jobId: 'thread-A' });
    expect(useAgentChatStore.getState().isConfirming).toBe(false);
    expect(useAgentChatStore.getState().isStreaming).toBe(false);
  });

  it('releases the composer when the durable confirm poll budget runs out (M3)', async () => {
    useAgentChatStore.setState({
      activeThreadId: 'thread-A',
      messages: [
        {
          id: 'a-1',
          role: 'assistant',
          content: 'Waiting for your confirmation...',
          timestamp: new Date(),
        },
      ],
      pendingConfirmations: {
        'thread-A': {
          threadId: 'thread-A',
          assistantMessageId: 'a-1',
          jobId: 'run-42',
          origin: 'durable',
          waitTokenId: 'wait-7',
          tools: [{ name: 'ingest_arxiv', args: {} }],
          message: 'Confirm?',
        },
      },
    });
    serviceMocks.streamConfirm.mockRejectedValueOnce(new Error('sse down'));

    vi.useFakeTimers();
    try {
      const p = useAgentChatStore.getState().confirmAction('thread-A', true);
      // 200 polls x 3s, plus slack for the awaits between them.
      await vi.advanceTimersByTimeAsync(200 * 3000 + 1000);
      await p;
    } finally {
      vi.useRealTimers();
    }

    expect(useAgentChatStore.getState().isConfirming).toBe(false);
    expect(useAgentChatStore.getState().isStreaming).toBe(false);
    expect(getAbortController()).toBeNull();
    // The card is gone: the approval already consumed its wait token, so
    // re-offering Approve would fail on every click.
    expect(
      useAgentChatStore.getState().pendingConfirmations['thread-A']
    ).toBeUndefined();
    expect(useAgentChatStore.getState().messages[0].content).toContain(
      'taking longer than expected'
    );
  });

  it('does not restore a confirmation into a transcript that was cleared', async () => {
    const pending = {
      threadId: 'thread-A',
      assistantMessageId: 'a-1',
      jobId: 'thread-A',
      origin: 'sse' as const,
      tools: [{ name: 'ingest_arxiv', args: {} }],
      message: 'Confirm?',
    };
    // Real aborts surface as a rejection out of streamConfirm.
    let failConfirm: (() => void) | undefined;
    serviceMocks.streamConfirm.mockImplementation(
      () =>
        new Promise<void>((_resolve, reject) => {
          failConfirm = () => reject(new Error('aborted'));
        })
    );
    useAgentChatStore.setState({
      activeThreadId: 'thread-A',
      messages: [
        {
          id: 'a-1',
          role: 'assistant',
          content: 'Waiting for your confirmation...',
          timestamp: new Date(),
        },
      ],
      pendingConfirmations: { 'thread-A': pending },
    });

    const confirming = useAgentChatStore
      .getState()
      .confirmAction('thread-A', true);
    // Let the dynamic service import resolve so streamConfirm is in flight.
    await new Promise((resolve) => setTimeout(resolve, 0));
    useAgentChatStore.getState().clearMessages();
    failConfirm?.();
    await confirming;

    expect(
      useAgentChatStore.getState().pendingConfirmations['thread-A']
    ).toBeUndefined();
  });

  it('aborts the in-flight generation when the transcript is cleared (M6)', () => {
    const controller = new AbortController();
    (
      useAgentChatStore.getState() as unknown as {
        _abortController: AbortController | null;
      }
    )._abortController = controller;

    useAgentChatStore.getState().clearMessages();

    expect(controller.signal.aborted).toBe(true);
    expect(getAbortController()).toBeNull();
  });

  it('cancels orphaned tool executions and drops the plan on stop (M7, R4-L20)', () => {
    useAgentChatStore.setState({
      messages: [
        {
          id: 'a-1',
          role: 'assistant',
          content: 'partial',
          timestamp: new Date(),
          isStreaming: true,
          toolExecutions: [
            {
              id: 'te-1',
              toolName: 'search_documents',
              toolDisplayName: 'Search Documents',
              args: {},
              status: 'running',
            },
          ],
        },
      ],
      currentPlan: [
        {
          step: 1,
          description: 'search',
          tool: 'search_documents',
          args_hint: {},
          depends_on: [],
        },
      ],
      isStreaming: true,
    });

    useAgentChatStore.getState().stopGeneration();

    const message = useAgentChatStore.getState().messages[0];
    expect(message.isStreaming).toBe(false);
    // R4-L20: a tool still running when the user stopped generation didn't
    // error — the turn was cut short. 'cancelled' says that, 'failed' lied.
    expect(message.toolExecutions?.[0].status).toBe('cancelled');
    expect(useAgentChatStore.getState().currentPlan).toBeNull();
  });
});
