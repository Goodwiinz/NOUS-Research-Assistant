import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ReviewVersionsPanel } from '../ReviewVersionsPanel';
import {
  createReviewVersion,
  listReviewVersions,
  type ProjectRoleAssignment,
  type ResearchProjectRole,
  type ReviewVersion,
  type ReviewVersionList,
} from '@/services/researchEngineService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  createReviewVersion: vi.fn(),
  ensureReviewWork: vi.fn(),
  exportReviewVersion: vi.fn(),
  getReviewAccounting: vi.fn(),
  listReviewVersions: vi.fn(),
}));

const role = (r: ResearchProjectRole, user = 'me'): ProjectRoleAssignment => ({
  id: `role-${r}-${user}`,
  project_id: 'p1',
  user_id: user,
  role: r,
  assigned_by_id: 'owner',
  created_at: '2026-10-01T00:00:00Z',
});

const base = {
  collection_id: 'p1',
  protocol_version_id: 'v1',
  strategy_version: null,
  report_ids: ['r1', 'r3', 'r4', 'r6'],
  prisma_body_hash: 'a'.repeat(64),
  content_hash: 'b'.repeat(64),
  created_by_id: 'me',
  created_at: '2026-10-01T00:00:00Z',
  release: null,
  stale_counts: {},
};

const root: ReviewVersion = {
  ...base,
  id: 'v-root',
  version_number: 1,
  parent_review_version_id: null,
  accepted_execution_id: null,
  delta_hash: null,
  carried: [],
  required_work: { title_abstract: [], full_text: [] },
  needs_attention: [],
  missing_history: [
    { report_id: 'r6', stage: 'title_abstract', kind: 'decision_missing' },
  ],
  rationale: 'Baseline review',
  is_tip: false,
  work: [
    {
      stage: 'title_abstract',
      status: 'none',
      required_report_ids: [],
      assigned_reviewer_ids: [],
      resolved_count: 0,
      unresolved_count: 0,
    },
  ],
};

const update: ReviewVersion = {
  ...base,
  id: 'v-2',
  version_number: 2,
  parent_review_version_id: 'v-root',
  accepted_execution_id: 'e1',
  delta_hash: 'd'.repeat(64),
  carried: [
    {
      report_id: 'r1',
      stage: 'title_abstract',
      resolution_id: 'res-1',
      event_id: 'ev-1',
      outcome: 'include',
      basis: 'agreement',
      uncertain: false,
    },
  ],
  required_work: { title_abstract: ['r4'], full_text: [] },
  needs_attention: [
    { report_id: 'r3', class: 'unknown', reason: 'not_returned' },
  ],
  missing_history: [],
  rationale: 'September search update',
  is_tip: true,
  stale_counts: { assessment: 2 },
  work: [
    {
      stage: 'title_abstract',
      status: 'queued',
      required_report_ids: ['r4'],
      queue_id: 'queue-abcdef12',
      assigned_reviewer_ids: ['rev'],
      resolved_count: 0,
      unresolved_count: 1,
    },
  ],
};

const listing: ReviewVersionList = {
  versions: [root, update],
  deltas: [
    {
      execution_id: 'e2',
      schedule_id: 's1',
      scheduled_local: '2026-10-12T06:00',
      baseline_execution_id: 'e1',
      delta_hash: 'f'.repeat(64),
      counts: {
        new: 2,
        changed: 1,
        corrected_retracted: 0,
        unchanged: 3,
        unknown: 1,
      },
    },
  ],
};

function renderPanel(roles: ProjectRoleAssignment[]): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ReviewVersionsPanel projectId="p1" roles={roles} />
    </QueryClientProvider>
  );
}

describe('ReviewVersionsPanel', () => {
  beforeEach(() => {
    vi.mocked(listReviewVersions).mockResolvedValue(listing);
    vi.mocked(createReviewVersion).mockResolvedValue(update);
  });

  it('unknown never counted as carried', async () => {
    renderPanel([role('supervisor')]);
    expect(
      await screen.findByText('1 decisions carried, 1 reports to review')
    ).toBeTruthy();
    const attention = screen.getByLabelText('Needs attention');
    expect(attention.textContent).toContain('r3');
    expect(attention.textContent).toContain('Not returned by this search');
    expect(screen.getByLabelText('Stale items').textContent).toContain(
      'Stale assessment: 2'
    );
  });

  it('missing history labelled', async () => {
    renderPanel([role('reviewer')]);
    const missing = await screen.findByLabelText('Missing history');
    expect(missing.textContent).toContain('r6');
    expect(missing.textContent).toContain('No recorded decision');
    expect(missing.textContent).not.toMatch(/exclude/i);
  });

  it('create hidden without supervisor', async () => {
    renderPanel([role('reviewer')]);
    expect(await screen.findByText('September search update')).toBeTruthy();
    expect(
      screen.queryByRole('button', { name: 'Create update from delta' })
    ).toBeNull();
  });

  it('supervisor creates an update from a delta with the reviewers', async () => {
    renderPanel([role('supervisor'), role('reviewer', 'rev')]);
    const select = await screen.findByLabelText('Accepted search delta');
    fireEvent.change(select, { target: { value: 'e2' } });
    fireEvent.change(screen.getByLabelText('Rationale'), {
      target: { value: 'October update' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Create update from delta' })
    );
    await waitFor(() => expect(createReviewVersion).toHaveBeenCalled());
    expect(vi.mocked(createReviewVersion).mock.calls[0][1]).toMatchObject({
      parent_review_version_id: 'v-2',
      execution_id: 'e2',
      delta_hash: 'f'.repeat(64),
      reviewer_user_ids: ['rev'],
      rationale: 'October update',
    });
  });
});
