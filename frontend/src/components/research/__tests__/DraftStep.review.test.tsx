import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DraftStep } from '../steps/DraftStep';
import { projectService } from '@/services/projectService';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listDrafts: vi.fn(),
    listDraftReviews: vi.fn(),
    generateDraft: vi.fn(),
    getGenerationStatus: vi.fn(),
  },
}));
vi.mock('../DraftGenerator', () => ({
  DraftGenerator: ({ onGenerate }: { onGenerate: (value: object) => void }) => (
    <button onClick={() => onGenerate({ themes: ['test'] })}>
      Generate now
    </button>
  ),
}));
vi.mock('../DraftGenerationProgress', () => ({
  DraftGenerationProgress: () => <div>Generating</div>,
}));
vi.mock('../ProjectChatTab', () => ({ ProjectChatTab: () => null }));

describe('DraftStep citation review', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the blocked candidate after terminal generation failure', async () => {
    vi.mocked(projectService.listDrafts).mockResolvedValue({
      drafts: [],
      total: 0,
      skip: 0,
      limit: 50,
    });
    vi.mocked(projectService.listDraftReviews)
      .mockResolvedValueOnce({ reviews: [] })
      .mockResolvedValue({
        reviews: [
          {
            id: 'review-1',
            project_id: 'project-1',
            base_draft_id: null,
            candidate_content_hash: 'a'.repeat(64),
            candidate_content: 'Fabricated fact.',
            source_document_ids: [],
            outcome: 'blocked',
            created_at: '2026-09-27T00:00:00Z',
            review: {
              docs_checked: 0,
              docs_skipped: 0,
              duration_ms: 1,
              fully_verified: false,
              summary: { major: 1 },
              uncited_assertions: [{ text: 'Fabricated fact.' }],
            },
          },
        ],
      });
    vi.mocked(projectService.generateDraft).mockResolvedValue({
      task_id: 'task-1',
      status: 'pending',
      message: 'started',
    });
    vi.mocked(projectService.getGenerationStatus).mockResolvedValue({
      task_id: 'task-1',
      status: 'failed',
      progress: 85,
      current_step: 'Citation review blocked persistence',
      started_at: '',
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <DraftStep
          projectId="project-1"
          documentCount={1}
          onContinue={vi.fn()}
          onBack={vi.fn()}
        />
      </QueryClientProvider>
    );

    fireEvent.click(
      await screen.findByRole('button', { name: 'Generate now' })
    );
    await waitFor(() =>
      expect(projectService.listDraftReviews).toHaveBeenCalledTimes(2)
    );
    expect(await screen.findByRole('status')).toHaveTextContent('blocked');
    expect(screen.getAllByText(/Fabricated fact/)).toHaveLength(2);
  });

  it('stops polling when the task is interrupted', async () => {
    vi.mocked(projectService.listDrafts).mockResolvedValue({
      drafts: [],
      total: 0,
      skip: 0,
      limit: 50,
    });
    vi.mocked(projectService.listDraftReviews).mockResolvedValue({
      reviews: [],
    });
    vi.mocked(projectService.generateDraft).mockResolvedValue({
      task_id: 'task-1',
      status: 'pending',
      message: 'started',
    });
    vi.mocked(projectService.getGenerationStatus).mockResolvedValue({
      task_id: 'task-1',
      status: 'interrupted',
      progress: 0,
      current_step: '',
      started_at: '',
      error_code: 'process_lost',
      state_source: 'database',
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <DraftStep
          projectId="project-1"
          documentCount={1}
          onContinue={vi.fn()}
          onBack={vi.fn()}
        />
      </QueryClientProvider>
    );

    fireEvent.click(
      await screen.findByRole('button', { name: 'Generate now' })
    );
    // The terminal branch refetches reviews instead of scheduling a poll.
    await waitFor(() =>
      expect(projectService.listDraftReviews).toHaveBeenCalledTimes(2)
    );
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(projectService.getGenerationStatus).toHaveBeenCalledTimes(1);
  });
});
