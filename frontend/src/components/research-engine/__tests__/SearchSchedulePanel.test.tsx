import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchSchedulePanel } from '../SearchSchedulePanel';
import {
  getSearchDelta,
  listSearchSchedules,
  type ProjectRoleAssignment,
  type ResearchProjectRole,
  type SearchDeltaExport,
  type SearchSchedule,
  type SearchScheduleList,
} from '@/services/researchEngineService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  createSearchSchedule: vi.fn(),
  exportSearchDelta: vi.fn(),
  getSearchDelta: vi.fn(),
  listSearchSchedules: vi.fn(),
  versionSearchSchedule: vi.fn(),
}));

const role = (r: ResearchProjectRole): ProjectRoleAssignment => ({
  id: `role-${r}`,
  project_id: 'p1',
  user_id: 'me',
  role: r,
  assigned_by_id: 'owner',
  created_at: '2026-10-01T00:00:00Z',
});

const counts = {
  new: 1,
  changed: 0,
  corrected_retracted: 1,
  unchanged: 0,
  unknown: 1,
};

const version: SearchSchedule['tip'] = {
  id: 's1',
  schedule_id: 's1',
  owner_id: 'me',
  protocol_version_id: 'v1',
  source_run_id: 'run-1',
  step_id: 'search',
  strategy_version: `sha256:${'a'.repeat(64)}`,
  query: 'membrane imaging',
  cron: '0 6 * * 1',
  timezone: 'Europe/London',
  enabled: true,
  supersedes_schedule_version_id: null,
  baseline_digest: 'b'.repeat(64),
  created_at: '2026-10-01T00:00:00Z',
};

const schedule: SearchSchedule = {
  schedule_id: 's1',
  tip: version,
  versions: [version],
  status: 'ok',
  next_fire_local: '2026-10-12T06:00',
  next_fire_utc: '2026-10-12T05:00:00Z',
  last_execution: {
    id: 'e1',
    schedule_id: 's1',
    schedule_version_id: 's1',
    scheduled_local: '2026-10-05T06:00',
    scheduled_for: '2026-10-05T05:00:00Z',
    missed_fires: 0,
    created_at: '2026-10-05T05:00:30Z',
    status: 'succeeded',
    attempts: [],
    import_receipt_id: 'r1',
    baseline_execution_id: null,
    delta_hash: 'd'.repeat(64),
    counts,
  },
};

const listing: SearchScheduleList = {
  schedules: [schedule],
  strategies: [
    {
      source_run_id: 'run-1',
      step_id: 'search',
      strategy_version: version.strategy_version,
      query: 'membrane imaging',
      providers: ['crossref'],
      protocol_version_id: 'v1',
      current_protocol: true,
    },
  ],
};

const delta: SearchDeltaExport = {
  schema: 'nous.academic.search-delta.v1',
  exported_at: '2026-10-05T06:01:00Z',
  body_sha256: 'e'.repeat(64),
  body: {
    execution_id: 'e1',
    collection_id: 'p1',
    schedule_id: 's1',
    schedule_version_id: 's1',
    scheduled_local: '2026-10-05T06:00',
    baseline_execution_id: null,
    baseline_digest: 'b'.repeat(64),
    corpus_snapshot_digest: 'c'.repeat(64),
    import_receipt_id: 'r1',
    delta_hash: 'd'.repeat(64),
    counts,
    items: [
      {
        report_id: 'r-d',
        class: 'unknown',
        reason: 'not_returned',
        evidence: {},
        publication: {},
      },
      {
        report_id: 'r-a',
        class: 'corrected_retracted',
        reason: null,
        evidence: {
          notices: [{ notice_doi: '10.5555/a.retraction', type: 'retraction' }],
        },
        publication: {},
      },
    ],
    coverage: {},
    citation_chasing: { required: false },
    filter: null,
    schedule_version: version,
    statement: 'Missing works are unknown.',
  },
};

function renderPanel(roles: ProjectRoleAssignment[]): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <SearchSchedulePanel projectId="p1" roles={roles} />
    </QueryClientProvider>
  );
}

describe('SearchSchedulePanel', () => {
  beforeEach(() => {
    vi.mocked(listSearchSchedules).mockResolvedValue(listing);
    vi.mocked(getSearchDelta).mockResolvedValue(delta);
  });

  it('unknown shows reason never deleted', async () => {
    renderPanel([role('supervisor')]);
    fireEvent.click(await screen.findByRole('button', { name: 'View delta' }));
    expect(await screen.findByText('Not returned by this search')).toBeTruthy();
    const link = screen.getByRole('link', { name: /retraction: 10.5555/ });
    expect(link.getAttribute('href')).toBe(
      'https://doi.org/10.5555/a.retraction'
    );
    expect(document.body.textContent ?? '').not.toMatch(/deleted/i);
  });

  it('create hidden without supervisor', async () => {
    renderPanel([role('reviewer')]);
    expect(await screen.findByText('membrane imaging')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save schedule' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Disable' })).toBeNull();
  });

  it('next fire shown in schedule timezone', async () => {
    renderPanel([role('supervisor')]);
    await waitFor(() =>
      expect(
        screen.getByText('Next run: 2026-10-12 06:00 Europe/London')
      ).toBeTruthy()
    );
    expect(screen.getByRole('button', { name: 'Save schedule' })).toBeTruthy();
    expect(screen.getByLabelText('Delta counts').textContent).toContain(
      'Corrected or retracted: 1'
    );
  });
});
