import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { DraftStatusPanel } from '../DraftStatusPanel';
import { projectService } from '@/services/projectService';

vi.mock('@/services/projectService', () => ({
  projectService: {
    getGenerationStatus: vi.fn(),
    cancelGeneration: vi.fn(),
  },
}));

const mocked = vi.mocked(projectService);

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const running = {
  task_id: 'abc123',
  status: 'generating',
  progress: 40,
  current_step: 'Generating content',
  started_at: new Date().toISOString(),
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('DraftStatusPanel', () => {
  it('shows the pending indicator while a draft generation runs', async () => {
    mocked.getGenerationStatus.mockResolvedValue(running);

    render(<DraftStatusPanel projectId="p1" />, { wrapper });

    expect(await screen.findByText('Drafting')).toBeInTheDocument();
    expect(screen.getByText('40%')).toBeInTheDocument();
    expect(screen.getAllByText('Generating content').length).toBeGreaterThan(0);
    expect(screen.getByRole('progressbar')).toHaveAttribute(
      'aria-valuenow',
      '40'
    );
  });

  it('renders nothing once the run completes', async () => {
    mocked.getGenerationStatus.mockResolvedValue({
      ...running,
      status: 'completed',
      progress: 100,
      current_step: 'Complete',
      draft_id: 'd1',
    });

    const { container } = render(<DraftStatusPanel projectId="p1" />, {
      wrapper,
    });

    await waitFor(() =>
      expect(mocked.getGenerationStatus).toHaveBeenCalledWith('p1')
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('keeps a failed run visible', async () => {
    mocked.getGenerationStatus.mockResolvedValue({
      ...running,
      status: 'failed',
      current_step: 'LLM timeout',
    });

    render(<DraftStatusPanel projectId="p1" />, { wrapper });

    expect(await screen.findByText('Draft failed')).toBeInTheDocument();
    expect(screen.getAllByText('LLM timeout').length).toBeGreaterThan(0);
  });

  it('renders nothing when the project has never generated (404)', async () => {
    mocked.getGenerationStatus.mockRejectedValue(new Error('Task not found'));

    const { container } = render(<DraftStatusPanel projectId="p1" />, {
      wrapper,
    });

    await waitFor(() =>
      expect(mocked.getGenerationStatus).toHaveBeenCalledWith('p1')
    );
    expect(container).toBeEmptyDOMElement();
  });
});
