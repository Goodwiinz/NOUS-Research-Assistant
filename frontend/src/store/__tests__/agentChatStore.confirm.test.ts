import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamConfirm: vi.fn(),
    confirmAction: vi.fn(),
    pollJob: vi.fn(),
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    getThreadMessages: vi.fn().mockResolvedValue({ messages: [], total: 0 }),
  },
  isTerminalJobStatus: vi.fn().mockReturnValue(true),
}));

import { act } from '@testing-library/react';
import type { QueryClient } from '@tanstack/react-query';
import { useAgentChatStore } from '@/store/agentChatStore';
import { agentChatService } from '@/services/agentChatService';
import { setAppQueryClient } from '@/lib/query-client';

const mockAgentChatService = vi.mocked(agentChatService);

const THREAD_ID = 'thread-1';
const ASSISTANT_MSG_ID = 'msg-assistant-1';
const invalidateQueries = vi.fn().mockResolvedValue(undefined);

function seedPendingConfirmation(): void {
  useAgentChatStore.setState({
    activeThreadId: THREAD_ID,
    messages: [
      {
        id: ASSISTANT_MSG_ID,
        role: 'assistant',
        content: 'Waiting for your confirmation...',
        timestamp: new Date(),
      },
    ],
    pendingConfirmations: {
      [THREAD_ID]: {
        threadId: THREAD_ID,
        assistantMessageId: ASSISTANT_MSG_ID,
        jobId: THREAD_ID,
        origin: 'sse',
        tools: [{ name: 'add_document_to_project', args: {} }],
        message: 'The agent wants to perform an action. Please confirm.',
      },
    },
  });
}

describe('agentChatStore confirmAction dead-run handling (R4-M26)', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    setAppQueryClient({
      invalidateQueries,
    } as unknown as QueryClient);
  });

  it('clears the confirmation card when the backend no longer awaits it', async () => {
    // backend/src/api/agent/streaming.py:2475 and :2617 emit this exact
    // message with category "conflict" once the run has finalized
    // (Stop during a parked confirm cancels it server-side) — Approve can
    // never succeed against a dead run, so the card must not come back.
    seedPendingConfirmation();
    mockAgentChatService.streamConfirm.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onError?.('Run is not awaiting confirmation', 'conflict');
      }
    );

    await act(async () => {
      await useAgentChatStore.getState().confirmAction(THREAD_ID, true);
    });

    expect(
      useAgentChatStore.getState().pendingConfirmations[THREAD_ID]
    ).toBeUndefined();
    const msg = useAgentChatStore
      .getState()
      .messages.find((m) => m.id === ASSISTANT_MSG_ID);
    // User-facing copy is decoupled from the raw backend string this branch
    // matches on (review point 5) — assert the friendly message, not the
    // regex-matched literal.
    expect(msg?.content).toBe(
      'This confirmation is no longer active — the run was stopped.'
    );
  });

  it('restores the card on a same-category conflict that is not the dead-run message', async () => {
    // Pins the regex contract: "Confirmation already in progress" (streaming.py
    // :2538/:2578) is ALSO category "conflict" but means a concurrent confirm
    // holds the claim — the run is still live. Matching on category alone
    // would wrongly wipe a perfectly recoverable card here.
    seedPendingConfirmation();
    mockAgentChatService.streamConfirm.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onError?.('Confirmation already in progress', 'conflict');
      }
    );

    await act(async () => {
      await useAgentChatStore.getState().confirmAction(THREAD_ID, true);
    });

    expect(
      useAgentChatStore.getState().pendingConfirmations[THREAD_ID]
    ).toEqual(
      expect.objectContaining({ threadId: THREAD_ID, jobId: THREAD_ID })
    );
  });

  it('still restores the card on a generic network failure', async () => {
    seedPendingConfirmation();
    mockAgentChatService.streamConfirm.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onError?.('Network error', undefined);
      }
    );

    await act(async () => {
      await useAgentChatStore.getState().confirmAction(THREAD_ID, true);
    });

    expect(
      useAgentChatStore.getState().pendingConfirmations[THREAD_ID]
    ).toEqual(
      expect.objectContaining({ threadId: THREAD_ID, jobId: THREAD_ID })
    );
  });

  it('settles a running tool when confirmation finishes without tool_end', async () => {
    seedPendingConfirmation();
    mockAgentChatService.streamConfirm.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('add_document_to_project', {});
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      await useAgentChatStore.getState().confirmAction(THREAD_ID, true);
    });

    expect(
      useAgentChatStore.getState().messages[0].toolExecutions?.[0].status
    ).toBe('failed');
  });

  it('invalidates the revision result project after confirmation resumes', async () => {
    seedPendingConfirmation();
    useAgentChatStore.getState().setPageContext({
      type: 'project',
      label: 'Bound project',
      projectId: 'p1',
    });
    mockAgentChatService.streamConfirm.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('revise_draft', {});
        callbacks.onToolEnd?.(
          'revise_draft',
          '{"status":"completed","project_id":"p2"}',
          false
        );
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      await useAgentChatStore.getState().confirmAction(THREAD_ID, true);
    });

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['project', 'p2'],
    });
  });

  it('uses the revision result project in the legacy polling fallback', async () => {
    seedPendingConfirmation();
    const pending =
      useAgentChatStore.getState().pendingConfirmations[THREAD_ID];
    pending.origin = undefined;
    useAgentChatStore.getState().setPageContext({
      type: 'project',
      label: 'Bound project',
      projectId: 'p1',
    });
    mockAgentChatService.streamConfirm.mockRejectedValue(
      new Error('stream unavailable')
    );
    mockAgentChatService.confirmAction.mockResolvedValue(undefined);
    mockAgentChatService.pollJob.mockResolvedValue({
      status: 'completed',
      result: {
        message: { role: 'assistant', content: 'Revision saved.' },
        tool_executions: [
          {
            id: 'revision-1',
            tool_name: 'revise_draft',
            tool_display_name: 'Revise draft',
            args: {},
            status: 'completed',
            result: { project_id: 'p2' },
          },
        ],
      },
    });
    const timeout = vi
      .spyOn(globalThis, 'setTimeout')
      .mockImplementation((callback: TimerHandler) => {
        if (typeof callback === 'function') callback();
        return 0 as unknown as ReturnType<typeof setTimeout>;
      });

    try {
      await act(async () => {
        await useAgentChatStore.getState().confirmAction(THREAD_ID, true);
      });
    } finally {
      timeout.mockRestore();
    }

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['project', 'p2'],
    });
  });
});
