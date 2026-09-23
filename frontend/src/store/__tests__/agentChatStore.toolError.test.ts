import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    startJob: vi.fn(),
    pollJob: vi.fn(),
    confirmAction: vi.fn(),
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    getThreadMessages: vi.fn().mockResolvedValue({ messages: [], total: 0 }),
  },
}));

import { act } from '@testing-library/react';
import type { QueryClient } from '@tanstack/react-query';
import { useAgentChatStore } from '@/store/agentChatStore';
import { agentChatService } from '@/services/agentChatService';
import { setAppQueryClient } from '@/lib/query-client';

const mockAgentChatService = vi.mocked(agentChatService);

function toolExecutions(): NonNullable<
  ReturnType<
    typeof useAgentChatStore.getState
  >['messages'][number]['toolExecutions']
> {
  const messages = useAgentChatStore.getState().messages;
  return messages[messages.length - 1]?.toolExecutions ?? [];
}

describe('agentChatStore tool_end error handling', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    setAppQueryClient({
      invalidateQueries: vi.fn().mockResolvedValue(undefined),
    } as unknown as QueryClient);
  });

  it('marks a tool execution failed when the frame carried is_error', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('search_documents', {});
        callbacks.onToolEnd?.('search_documents', 'boom', true);
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('search');
      await useAgentChatStore.getState().sendMessage();
    });

    const execs = toolExecutions();
    expect(execs).toHaveLength(1);
    expect(execs[0].status).toBe('failed');
    expect(execs[0].error).toBe('boom');
  });

  it('still completes a tool execution when is_error is absent', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('search_documents', {});
        callbacks.onToolEnd?.('search_documents', '{"ok":true}');
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('search');
      await useAgentChatStore.getState().sendMessage();
    });

    const execs = toolExecutions();
    expect(execs[0].status).toBe('completed');
    expect(execs[0].error).toBeUndefined();
  });

  it('keeps a successful frame pending when the result payload is pending', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('create_draft', {});
        callbacks.onToolEnd?.(
          'create_draft',
          '{"status":"pending","task_id":"draft-task"}',
          false
        );
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('create a draft');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(toolExecutions()[0].status).toBe('pending');
  });

  it('settles a running tool when the stream finishes without tool_end', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('search_documents', {});
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('search');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(toolExecutions()[0].status).toBe('failed');
  });

  it('keeps a missing SSE tool name from dropping the step', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.(undefined as unknown as string, {});
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('search');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(toolExecutions()[0]).toEqual(
      expect.objectContaining({
        toolDisplayName: 'Unknown tool',
        status: 'failed',
      })
    );
  });

  it('settles each same-tool execution separately and gives them unique ids', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('search_documents', {});
        callbacks.onToolStart?.('search_documents', {});
        callbacks.onToolEnd?.('search_documents', 'first failed', true);
        callbacks.onToolEnd?.('search_documents', '{"ok":true}', false);
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('search twice');
      await useAgentChatStore.getState().sendMessage();
    });

    const execs = toolExecutions();
    expect(execs).toHaveLength(2);
    expect(new Set(execs.map((te) => te.id)).size).toBe(2);
    expect(execs.map((te) => te.status).sort()).toEqual([
      'completed',
      'failed',
    ]);
  });
});
