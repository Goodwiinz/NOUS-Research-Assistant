import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listConversations: vi.fn(),
    listThreads: vi.fn(),
  },
}));

import { workspaceService } from '@/services/workspaceService';
import { LinkThreadModal } from '../LinkThreadModal';

async function submitWith(onLinkThread: () => Promise<void>): Promise<void> {
  render(
    <LinkThreadModal
      isOpen
      projectWorkspaceId="ws-1"
      onClose={vi.fn()}
      onLinkThread={onLinkThread}
    />
  );
  fireEvent.click(await screen.findByText('Thread one'));
  fireEvent.click(screen.getByRole('button', { name: /link thread/i }));
}

describe('LinkThreadModal submit errors', () => {
  beforeEach(() => {
    vi.mocked(workspaceService.listConversations).mockResolvedValue({
      conversations: [{ id: 'conv-1' }],
    } as Awaited<ReturnType<typeof workspaceService.listConversations>>);
    vi.mocked(workspaceService.listThreads).mockResolvedValue({
      threads: [
        {
          id: 'thread-1',
          title: 'Thread one',
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    } as Awaited<ReturnType<typeof workspaceService.listThreads>>);
  });

  it('maps an "already linked" error to a friendly message', async () => {
    await submitWith(() =>
      Promise.reject(new Error('Thread already linked to project'))
    );

    expect(
      await screen.findByText('This thread is already linked to this project')
    ).toBeInTheDocument();
  });

  it('falls back to a generic message for non-Error rejections', async () => {
    await submitWith(() => Promise.reject('boom'));

    expect(
      await screen.findByText('Failed to link thread')
    ).toBeInTheDocument();
  });

  it('filters the thread list by the search query', async () => {
    vi.mocked(workspaceService.listThreads).mockResolvedValue({
      threads: [
        { id: 'thread-1', title: 'Thread one', created_at: '2026-01-01' },
        { id: 'thread-2', title: 'Other topic', created_at: '2026-01-02' },
      ],
    } as Awaited<ReturnType<typeof workspaceService.listThreads>>);
    render(
      <LinkThreadModal
        isOpen
        projectWorkspaceId="ws-1"
        onClose={vi.fn()}
        onLinkThread={vi.fn()}
      />
    );
    await screen.findByText('Other topic');

    fireEvent.change(screen.getByPlaceholderText('Search threads...'), {
      target: { value: 'other' },
    });

    expect(screen.getByText('Other topic')).toBeInTheDocument();
    expect(screen.queryByText('Thread one')).toBeNull();
  });
});
