import {
  QueryClient,
  QueryClientProvider,
  useMutation,
} from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { JourneyRail } from '../JourneyRail';
import {
  downloadAuditBundle,
  getJourney,
} from '@/services/researchEngineService';
import type {
  ApiJourneyResponse,
  ApiJourneyStage,
} from '@/types/api/research-journey-contract';

vi.mock('@/services/researchEngineService', () => ({
  getJourney: vi.fn(),
  downloadAuditBundle: vi.fn(),
}));

function stage(
  key: ApiJourneyStage['key'],
  status: ApiJourneyStage['status'],
  overrides: Partial<ApiJourneyStage> = {}
): ApiJourneyStage {
  return { key, status, facts: {}, blockers: [], ...overrides };
}

const JOURNEY: ApiJourneyResponse = {
  stages: [
    stage('plan', 'complete', { facts: { question_versions: 1 } }),
    stage('discover', 'complete'),
    stage('select', 'attention', {
      facts: { queued_reports: 4, open_conflicts: 1, prisma_error: null },
      blockers: ['1 open conflict(s)'],
    }),
    stage('extract', 'in_progress'),
    stage('write', 'not_started'),
  ],
  current: 'select',
};

function MutateButton(): React.ReactElement {
  const mutation = useMutation({ mutationFn: async () => 'done' });
  return (
    <button type="button" onClick={() => mutation.mutate()}>
      Mutate
    </button>
  );
}

function renderRail(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <JourneyRail projectId="collection-1" />
      <MutateButton />
    </QueryClientProvider>
  );
}

describe('JourneyRail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getJourney).mockResolvedValue(JOURNEY);
  });

  it('renders five stages with status text and marks current step', async () => {
    renderRail();
    const rail = await screen.findByRole('list', { name: 'Research journey' });
    await waitFor(() =>
      expect(rail.querySelectorAll(':scope > li')).toHaveLength(5)
    );
    expect(screen.getAllByText('Complete')).toHaveLength(2);
    expect(screen.getByText('Needs attention')).toBeInTheDocument();
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText('Not started')).toBeInTheDocument();
    expect(
      screen.getByText('queued reports 4 · open conflicts 1')
    ).toBeInTheDocument();
    expect(screen.getByText('1 open conflict(s)')).toBeInTheDocument();
    const current = rail.querySelector('[aria-current="step"]');
    expect(current).toHaveTextContent('Select');
    expect(screen.getByRole('link', { name: 'Write' })).toHaveAttribute(
      'href',
      '#journey-write'
    );
    expect(getJourney).toHaveBeenCalledWith('collection-1');
  });

  it('download button calls downloadAuditBundle', async () => {
    vi.mocked(downloadAuditBundle).mockResolvedValue();
    renderRail();
    fireEvent.click(
      screen.getByRole('button', { name: 'Download audit bundle' })
    );
    await waitFor(() =>
      expect(downloadAuditBundle).toHaveBeenCalledWith('collection-1')
    );
  });

  it('refetches journey after a mutation settles', async () => {
    renderRail();
    await screen.findByText('Needs attention');
    expect(getJourney).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Mutate' }));
    await waitFor(() => expect(getJourney).toHaveBeenCalledTimes(2));
  });
});
