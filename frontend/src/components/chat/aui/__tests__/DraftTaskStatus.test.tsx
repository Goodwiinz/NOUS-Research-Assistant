import { afterEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';

import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';
import { AuiMessageByIndex } from '../AuiMessage';

const getGenerationStatus = vi.fn();
vi.mock('@/services/projectService', () => ({
  projectService: {
    getGenerationStatus: (...args: unknown[]) => getGenerationStatus(...args),
  },
}));

const projectId = 'c817d5bc-6560-4000-8000-000000000001';
const draftId = 'd817d5bc-6560-4000-8000-000000000002';
const taskId = 'dfb4d4b84b3e';

function renderDraftMessage(
  result: unknown,
  content = `Draft generation started.\n\nStatus: pending\nTask ID: ${taskId}`
) {
  const message = makeChatPageMessage({
    id: 'assistant-draft',
    role: 'assistant',
    content,
    toolExecutions: [
      {
        tool: 'create_draft',
        label: 'Draft synthesis',
        status: 'done',
        result,
      },
    ],
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ChatRuntimeProvider
        messages={[message]}
        isRunning={false}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      >
        <AuiMessageByIndex index={0} message={message} />
      </ChatRuntimeProvider>
    </QueryClientProvider>
  );
}

describe('draft task status in chat', () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it('replaces a stale pending line with the live stage from the task', async () => {
    getGenerationStatus.mockResolvedValue({
      task_id: taskId,
      status: 'analyzing',
      current_step: 'Analyzing documents',
      progress: 10,
      started_at: '2026-09-22T07:45:00',
    });
    renderDraftMessage({
      task_id: taskId,
      project_id: projectId,
      status: 'pending',
    });

    expect(
      await screen.findByText('Reading project sources')
    ).toBeInTheDocument();
    expect(screen.queryByText(/Status: pending/)).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Reading project sources'
    );
    expect(screen.getByRole('progressbar')).not.toHaveAttribute(
      'aria-valuenow'
    );
    expect(getGenerationStatus).toHaveBeenCalledWith(projectId, taskId);
  });

  it('shows completion and a direct draft link after a background status change', async () => {
    getGenerationStatus
      .mockResolvedValueOnce({
        task_id: taskId,
        status: 'generating',
        current_step: 'Generating content',
        progress: 30,
        started_at: '2026-09-22T07:45:00',
      })
      .mockResolvedValue({
        task_id: taskId,
        status: 'completed',
        current_step: 'Draft completed',
        progress: 100,
        draft_id: draftId,
        started_at: '2026-09-22T07:45:00',
      });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderDraftMessage(
      JSON.stringify({ task_id: taskId, project_id: projectId })
    );

    expect(await screen.findByText('Writing draft')).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(5000);
    await waitFor(() =>
      expect(screen.getByText('Draft ready')).toBeInTheDocument()
    );
    expect(screen.getByRole('link', { name: 'View draft' })).toHaveAttribute(
      'href',
      `/projects/${projectId}?tab=drafts&draftId=${draftId}`
    );
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(10000);
    expect(getGenerationStatus).toHaveBeenCalledTimes(2);
  });

  it('shows a failed task without a completion link', async () => {
    getGenerationStatus.mockResolvedValue({
      task_id: taskId,
      status: 'failed',
      current_step: 'Internal failure detail',
      progress: 0,
      started_at: '2026-09-22T07:45:00',
    });
    renderDraftMessage({ task_id: taskId, project_id: projectId });

    expect(await screen.findByText('Draft failed')).toBeInTheDocument();
    expect(
      screen.queryByText('Internal failure detail')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'View draft' })
    ).not.toBeInTheDocument();
  });

  it('never treats an unavailable status read as success', async () => {
    getGenerationStatus.mockRejectedValue(new Error('internal backend detail'));
    renderDraftMessage({ task_id: taskId, project_id: projectId });

    expect(await screen.findByText('Status unavailable')).toBeInTheDocument();
    expect(
      screen.queryByText('internal backend detail')
    ).not.toBeInTheDocument();
    expect(screen.queryByText('Draft ready')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open drafts' })).toHaveAttribute(
      'href',
      `/projects/${projectId}?tab=drafts`
    );
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(getGenerationStatus).toHaveBeenCalledTimes(2));
  });

  it('does not open a status request for an unrelated tool result', () => {
    renderDraftMessage({ message: 'done' }, 'A draft was discussed.');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(getGenerationStatus).not.toHaveBeenCalled();
  });

  it('tracks every draft started by one assistant turn', async () => {
    const secondTaskId = 'edc5e5c95840';
    getGenerationStatus.mockImplementation((_projectId: string, id: string) =>
      Promise.resolve({
        task_id: id,
        status: id === taskId ? 'generating' : 'completed',
        current_step: id === taskId ? 'Generating content' : 'Draft completed',
        progress: id === taskId ? 30 : 100,
        draft_id: id === secondTaskId ? draftId : undefined,
        started_at: '2026-09-22T07:45:00',
      })
    );
    const message = makeChatPageMessage({
      id: 'assistant-two-drafts',
      role: 'assistant',
      content: 'Started two drafts.',
      toolExecutions: [taskId, secondTaskId].map((id) => ({
        tool: 'create_draft',
        label: 'Draft synthesis',
        status: 'done' as const,
        result: { task_id: id, project_id: projectId },
      })),
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });

    render(
      <QueryClientProvider client={client}>
        <ChatRuntimeProvider
          messages={[message]}
          isRunning={false}
          onSend={vi.fn()}
          onCancel={vi.fn()}
        >
          <AuiMessageByIndex index={0} message={message} />
        </ChatRuntimeProvider>
      </QueryClientProvider>
    );

    expect(await screen.findByText('Draft ready')).toBeInTheDocument();
    expect(screen.getByText('Writing draft')).toBeInTheDocument();
    expect(
      screen.getAllByRole('region', { name: 'Draft generation' })
    ).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'View draft' })).toHaveAttribute(
      'href',
      `/projects/${projectId}?tab=drafts&draftId=${draftId}`
    );
  });
});
