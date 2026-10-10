import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    startJob: vi.fn(),
    pollJob: vi.fn(),
    confirmAction: vi.fn(),
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    getThreadMessages: vi.fn().mockResolvedValue({
      messages: [],
      total: 0,
    }),
  },
}));

import { act } from '@testing-library/react';
import type { QueryClient } from '@tanstack/react-query';
import { useAgentChatStore } from '@/store/agentChatStore';
import { agentChatService } from '@/services/agentChatService';
import { setAppQueryClient } from '@/lib/query-client';

const mockAgentChatService = vi.mocked(agentChatService);

const invalidateQueries = vi.fn().mockResolvedValue(undefined);

describe('agentChatStore project sync', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    setAppQueryClient({ invalidateQueries } as unknown as QueryClient);
    mockAgentChatService.listThreads.mockResolvedValue({
      threads: [],
      total: 0,
    });
    mockAgentChatService.getThreadMessages.mockResolvedValue({
      messages: [],
      total: 0,
    });
  });

  it('increments projectDataVersion for create_project_note tool executions in SSE mode', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('create_project_note', {});
        callbacks.onToolEnd?.('create_project_note', '{"ok":true}');
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('create a note');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(useAgentChatStore.getState().projectDataVersion).toBe(1);
  });

  it('increments projectDataVersion only once for a single mutating SSE tool execution', async () => {
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('add_document_to_project', {});
        callbacks.onToolEnd?.('add_document_to_project', '{"ok":true}');
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('add the paper');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(useAgentChatStore.getState().projectDataVersion).toBe(1);
  });

  it('invalidates the Query-side project cache scoped to the bound project', async () => {
    useAgentChatStore.getState().setPageContext({
      type: 'project',
      label: 'Project X',
      projectId: 'p1',
    });
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('create_project_note', {});
        callbacks.onToolEnd?.('create_project_note', '{"ok":true}');
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('create a note');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['project', 'p1'],
    });
  });

  it('invalidates project draft queries after a completed revision', async () => {
    useAgentChatStore.getState().setPageContext({
      type: 'project',
      label: 'Project X',
      projectId: 'p1',
    });
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('revise_draft', {});
        callbacks.onToolEnd?.(
          'revise_draft',
          '{"status":"completed","version":4,"project_id":"p2"}'
        );
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('revise the draft');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['project', 'p2'],
    });
  });

  it('falls back to the bound project for a legacy revision result', async () => {
    useAgentChatStore.getState().setPageContext({
      type: 'project',
      label: 'Project X',
      projectId: 'p1',
    });
    mockAgentChatService.streamMessage.mockImplementation(
      async (_request, callbacks) => {
        callbacks.onToolStart?.('revise_draft', {});
        callbacks.onToolEnd?.(
          'revise_draft',
          '{"status":"completed","version":4}'
        );
        callbacks.onDone?.();
      }
    );

    await act(async () => {
      useAgentChatStore.getState().setInputValue('revise the draft');
      await useAgentChatStore.getState().sendMessage();
    });

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['project', 'p1'],
    });
  });

  it('does not invalidate the Query cache for non-mutating tools', async () => {
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

    expect(invalidateQueries).not.toHaveBeenCalled();
  });
});
