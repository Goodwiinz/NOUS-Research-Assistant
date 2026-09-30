import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ReportIdentityPanel } from '../ReportIdentityPanel';
import {
  linkStudy,
  listReportHistory,
  listReports,
  mergeReports,
  type ResearchReport,
} from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  listReports: vi.fn(),
  listReportHistory: vi.fn(),
  linkStudy: vi.fn(),
  mergeReports: vi.fn(),
}));

const report = (
  id: string,
  title: string,
  overrides: Partial<ResearchReport> = {}
): ResearchReport => ({
  id,
  title_snapshot: title,
  identifiers: { doi: [`10.1000/${id}`] },
  study_id: null,
  study_link_status: null,
  observations: [
    {
      source_id: `source-${id}`,
      run_id: 'run-1',
      match_method: 'doi',
      evidence: {
        observed: { doi: `10.1000/${id}` },
        conflicts: [{ kind: 'pmid', value: '99', report_key: 'other' }],
      },
    },
  ],
  ...overrides,
});

function renderPanel(readOnly = false): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ReportIdentityPanel projectId="collection-1" readOnly={readOnly} />
    </QueryClientProvider>
  );
  return client;
}

describe('ReportIdentityPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listReports).mockResolvedValue([
      report('r1', 'Deep residual learning', {
        study_id: 'study-1',
        study_link_status: 'proposed',
      }),
      report('r2', 'Deep residual learning (preprint)'),
    ]);
    vi.mocked(listReportHistory).mockResolvedValue([
      {
        seq: 1,
        event_type: 'identity.study_linked',
        actor_user_id: 'reviewer-1',
        actor_role: 'reviewer',
        reason: 'same trial',
        payload: { report_id: 'r1', status: 'proposed' },
        occurred_at: '2026-09-29T00:00:00Z',
      },
    ]);
  });

  it('lists reports with identifiers, observation count and study status', async () => {
    renderPanel();

    expect(
      await screen.findByText('Deep residual learning')
    ).toBeInTheDocument();
    expect(listReports).toHaveBeenCalledWith('collection-1');
    expect(screen.getByText('doi: 10.1000/r1')).toBeInTheDocument();
    expect(screen.getByText('proposed')).toBeInTheDocument();
  });

  it('shows match evidence, identifier conflicts and history in the disclosure', async () => {
    renderPanel();

    const [evidence] = await screen.findAllByText('Evidence');
    fireEvent.click(evidence);

    expect(screen.getAllByText(/doi match/)[0]).toBeInTheDocument();
    expect(screen.getAllByText('Conflict: pmid 99')[0]).toBeInTheDocument();
    expect(
      await screen.findByText(/identity\.study_linked by reviewer/)
    ).toBeInTheDocument();
  });

  it('confirms a study link with a rationale and refreshes reports', async () => {
    vi.mocked(linkStudy).mockResolvedValue(
      report('r1', 'Deep residual learning', {
        study_id: 'study-1',
        study_link_status: 'confirmed',
      })
    );
    const client = renderPanel();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    await screen.findByText('Deep residual learning');

    const confirm = screen.getByRole('button', {
      name: 'Confirm Deep residual learning',
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(
      screen.getByLabelText('Rationale for Deep residual learning'),
      { target: { value: 'registry matches' } }
    );
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(linkStudy).toHaveBeenCalledWith(
        'collection-1',
        'r1',
        expect.objectContaining({
          status: 'confirmed',
          rationale: 'registry matches',
          idempotency_key: expect.any(String),
        })
      )
    );
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['research-reports', 'collection-1'],
      })
    );
  });

  it('offers Dispute only on a report that already has a study', async () => {
    renderPanel();
    await screen.findByText('Deep residual learning');

    expect(
      screen.getByRole('button', { name: 'Dispute Deep residual learning' })
    ).toBeInTheDocument();
    const preprint = 'Deep residual learning (preprint)';
    expect(
      screen.getByRole('button', { name: `Confirm ${preprint}` })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: `Dispute ${preprint}` })
    ).not.toBeInTheDocument();
  });

  it('surfaces a history failure even when reports load', async () => {
    vi.mocked(listReportHistory).mockRejectedValue(
      new Error('history unavailable')
    );
    renderPanel();

    expect(
      await screen.findByText('Deep residual learning')
    ).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'history unavailable'
    );
  });

  it('merges into another report and surfaces the API error', async () => {
    vi.mocked(mergeReports).mockRejectedValue(
      new Error('adjudicator role required')
    );
    renderPanel();
    await screen.findByText('Deep residual learning');

    const preprint = 'Deep residual learning (preprint)';
    fireEvent.change(screen.getByLabelText(`Rationale for ${preprint}`), {
      target: { value: 'duplicate' },
    });
    fireEvent.change(screen.getByLabelText(`Merge ${preprint} into`), {
      target: { value: 'r1' },
    });
    fireEvent.click(screen.getByRole('button', { name: `Merge ${preprint}` }));

    await waitFor(() =>
      expect(mergeReports).toHaveBeenCalledWith(
        'collection-1',
        expect.objectContaining({
          surviving_report_id: 'r1',
          merged_report_ids: ['r2'],
          rationale: 'duplicate',
        })
      )
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'adjudicator role required'
    );
  });
});

describe('ReportIdentityPanel read-only', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listReports).mockResolvedValue([report('r1', 'Archived paper')]);
    vi.mocked(listReportHistory).mockResolvedValue([]);
  });

  it('hides the Decision column and every action', async () => {
    renderPanel(true);

    expect(await screen.findByText('Archived paper')).toBeInTheDocument();
    expect(
      screen.queryByRole('columnheader', { name: 'Decision' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText('Rationale for Archived paper')
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
