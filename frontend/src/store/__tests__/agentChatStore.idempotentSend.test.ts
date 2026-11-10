/**
 * A4-AG3: the Global Agent Chat must (1) tag the new turn with a
 * client_message_id so backend idempotency dedupes transport retries, and
 * (2) never start a second durable run once the SSE run was accepted.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  STREAM_INTERRUPTED_NOTE,
  trackStreamAcceptance,
  useAgentChatStore,
} from '@/store/agentChatStore';
import type {
  AgentExecuteRequest,
  AgentStreamCallbacks,
} from '@/services/agentChatService';

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

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function lastUserTurn(request: AgentExecuteRequest) {
  return [...request.messages].reverse().find((m) => m.role === 'user');
}

async function sendAndDrain(text: string): Promise<void> {
  useAgentChatStore.getState().setInputValue(text);
  vi.useFakeTimers();
  try {
    const send = useAgentChatStore.getState().sendMessage();
    await vi.advanceTimersByTimeAsync(200 * 3000 + 1000);
    await send;
  } finally {
    vi.useRealTimers();
  }
}

describe('agentChatStore idempotent send (A4-AG3)', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    serviceMocks.getThreadMessages.mockResolvedValue({ messages: [] });
    serviceMocks.getDurableRunStatus.mockResolvedValue({
      status: 'COMPLETED',
      output: { status: 'done' },
    } as never);
  });

  it('sends a fresh client_message_id on the new user turn only', async () => {
    const seen: AgentExecuteRequest[] = [];
    serviceMocks.streamMessage.mockImplementation((async (
      request: AgentExecuteRequest,
      callbacks: AgentStreamCallbacks
    ) => {
      seen.push(request);
      callbacks.onDone?.();
    }) as never);

    useAgentChatStore.getState().setInputValue('first');
    await useAgentChatStore.getState().sendMessage();
    useAgentChatStore.getState().setInputValue('second');
    await useAgentChatStore.getState().sendMessage();

    expect(seen).toHaveLength(2);
    const firstId = lastUserTurn(seen[0])?.client_message_id;
    const secondId = lastUserTurn(seen[1])?.client_message_id;
    expect(firstId).toMatch(UUID_RE);
    expect(secondId).toMatch(UUID_RE);
    expect(secondId).not.toBe(firstId);
    // History is context, not a new submission: no id on earlier turns.
    const history = seen[1].messages.slice(0, -1);
    expect(history.every((m) => m.client_message_id === undefined)).toBe(true);
  });

  it('does not start a durable run after the SSE run was accepted', async () => {
    serviceMocks.streamMessage.mockImplementation((async (
      _req: unknown,
      callbacks: AgentStreamCallbacks
    ) => {
      callbacks.onRunId?.('run-accepted');
      callbacks.onToken?.('partial ');
      throw new Error('socket hang up');
    }) as never);

    useAgentChatStore.getState().setInputValue('hello');
    await useAgentChatStore.getState().sendMessage();

    expect(serviceMocks.startDurableRun).not.toHaveBeenCalled();
    const assistants = useAgentChatStore
      .getState()
      .messages.filter((m) => m.role === 'assistant');
    expect(assistants).toHaveLength(1);
    expect(assistants[0].isStreaming).toBe(false);
    expect(assistants[0].content).toContain('partial');
    expect(assistants[0].content).toContain(STREAM_INTERRUPTED_NOTE);
    expect(useAgentChatStore.getState().isStreaming).toBe(false);
  });

  it('treats a bare status frame as acceptance', async () => {
    serviceMocks.streamMessage.mockImplementation((async (
      _req: unknown,
      callbacks: AgentStreamCallbacks
    ) => {
      callbacks.onStatus?.('thinking' as never);
      throw new Error('socket hang up');
    }) as never);

    useAgentChatStore.getState().setInputValue('hello');
    await useAgentChatStore.getState().sendMessage();

    expect(serviceMocks.startDurableRun).not.toHaveBeenCalled();
    const assistant = useAgentChatStore
      .getState()
      .messages.find((m) => m.role === 'assistant');
    expect(assistant?.content).toBe(STREAM_INTERRUPTED_NOTE);
  });

  it('falls back to one durable run with the same id when the stream never connected', async () => {
    let streamedId: string | null | undefined;
    serviceMocks.streamMessage.mockImplementation((async (
      request: AgentExecuteRequest,
      callbacks: AgentStreamCallbacks
    ) => {
      streamedId = lastUserTurn(request)?.client_message_id;
      callbacks.onAuthRefreshAttempt?.();
      throw new TypeError('Failed to fetch');
    }) as never);

    await sendAndDrain('hello');

    expect(serviceMocks.startDurableRun).toHaveBeenCalledTimes(1);
    const durable = (
      serviceMocks.startDurableRun.mock.calls[0] as unknown as [
        AgentExecuteRequest,
      ]
    )[0];
    expect(streamedId).toMatch(UUID_RE);
    expect(lastUserTurn(durable)?.client_message_id).toBe(streamedId);
  });
});

describe('trackStreamAcceptance', () => {
  it('marks acceptance for frames but not for auth retries or errors', () => {
    const accepted = vi.fn();
    const onError = vi.fn();
    const onToken = vi.fn();
    const wrapped = trackStreamAcceptance({ onError, onToken }, accepted);

    wrapped.onAuthRefreshAttempt?.();
    wrapped.onError?.('Stream failed: 503');
    expect(accepted).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith('Stream failed: 503');

    wrapped.onSeq?.(1);
    expect(accepted).toHaveBeenCalledTimes(1);
    wrapped.onToken?.('x');
    expect(onToken).toHaveBeenCalledWith('x');
    expect(accepted).toHaveBeenCalledTimes(2);
  });
});
