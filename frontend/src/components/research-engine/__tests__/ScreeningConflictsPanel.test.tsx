import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ScreeningConflictsPanel } from '../ScreeningConflictsPanel';
import { APIErrorClass } from '@/types/api';
import {
  adjudicateScreening,
  listScreeningConflicts,
  listScreeningQueues,
  reopenScreening,
  type ProjectRoleAssignment,
  type ScreeningConflict,
  type ScreeningQueue,
} from '@/services/researchEngineService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  listScreeningQueues: vi.fn(),
  listScreeningConflicts: vi.fn(),
  adjudicateScreening: vi.fn(),
  reopenScreening: vi.fn(),
}));

const HASH = 'c'.repeat(64);

const queue = (overrides: Partial<ScreeningQueue> = {}): ScreeningQueue => ({
  id: 'queue-1',
  stage: 'title_abstract',
  protocol_version_id: 'version-1',
  criteria_hash: HASH,
  reviewer_mode: 'dual_independent',
  created_by_id: 'sup',
  created_at: '2026-09-29T00:00:00Z',
  report_count: 2,
  assignment_count: 2,
  observation_count: 2,
  suggestion_count: 0,
  resolved_count: 0,
  conflict_count: 1,
  ...overrides,
});

const observation = (
  id: string,
  reviewer: string,
  decision: 'include' | 'exclude',
  note?: string
): ScreeningConflict['observations'][number] => ({
  id,
  queue_id: 'queue-1',
  report_id: 'report-1',
  reviewer_id: reviewer,
  assignment_id: `a-${reviewer}`,
  decision,
  note: note ?? null,
  created_at: '2026-09-29T00:00:00Z',
});

const conflict: ScreeningConflict = {
  report_id: 'report-1',
  title_snapshot: 'Alpha trial',
  identifiers: { doi: ['10.1000/alpha'] },
  exclusion_reasons: ['wrong population', 'wrong design'],
  resolution: {
    id: 'tip-1',
    report_id: 'report-1',
    basis: 'conflict',
    input_observation_ids: ['obs-a', 'obs-b'],
    criteria_hash: HASH,
    created_at: '2026-09-29T00:00:00Z',
  },
  observations: [
    observation('obs-a', 'r1', 'include', 'fits'),
    observation('obs-b', 'r2', 'exclude'),
  ],
};

const role = (
  userId: string,
  name: ProjectRoleAssignment['role']
): ProjectRoleAssignment => ({
  id: `${userId}-${name}`,
  project_id: 'project-1',
  user_id: userId,
  role: name,
  assigned_by_id: 'owner',
  created_at: '2026-09-29T00:00:00Z',
});

function renderPanel(
  roles: ProjectRoleAssignment[],
  readOnly = false
): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ScreeningConflictsPanel
        projectId="project-1"
        roles={roles}
        readOnly={readOnly}
      />
    </QueryClientProvider>
  );
  return client;
}

const apiError = (status: number, message: string): APIErrorClass =>
  new APIErrorClass({ message, status_code: status } as ConstructorParameters<
    typeof APIErrorClass
  >[0]);

describe('ScreeningConflictsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listScreeningQueues).mockResolvedValue([queue()]);
    vi.mocked(listScreeningConflicts).mockResolvedValue([conflict]);
    vi.mocked(adjudicateScreening).mockResolvedValue(
      {} as Awaited<ReturnType<typeof adjudicateScreening>>
    );
    vi.mocked(reopenScreening).mockResolvedValue(
      {} as Awaited<ReturnType<typeof reopenScreening>>
    );
  });

  it('renders nothing for a non-adjudicator (owners and supervisors too)', () => {
    renderPanel([role('me', 'supervisor'), role('me', 'reviewer')]);

    expect(screen.queryByText('Screening conflicts')).not.toBeInTheDocument();
    expect(listScreeningQueues).not.toHaveBeenCalled();
    expect(listScreeningConflicts).not.toHaveBeenCalled();
  });

  it('lists conflicts with identifiers and both observations side by side', async () => {
    renderPanel([role('me', 'adjudicator')]);

    expect(await screen.findByText('Alpha trial')).toBeInTheDocument();
    expect(screen.getByText('doi: 10.1000/alpha')).toBeInTheDocument();
    const observations = screen.getByLabelText('Observations for Alpha trial');
    expect(observations).toHaveTextContent('Reviewer r1');
    expect(observations).toHaveTextContent('include');
    expect(observations).toHaveTextContent('fits');
    expect(observations).toHaveTextContent('Reviewer r2');
    expect(observations).toHaveTextContent('exclude');
    expect(listScreeningConflicts).toHaveBeenCalledWith('project-1', 'queue-1');
  });

  it('skips queues without conflicts', async () => {
    vi.mocked(listScreeningQueues).mockResolvedValue([
      queue({ conflict_count: 0 }),
    ]);
    renderPanel([role('me', 'adjudicator')]);

    expect(await screen.findByText('No open conflicts.')).toBeInTheDocument();
    expect(listScreeningConflicts).not.toHaveBeenCalled();
  });

  it('adjudicate needs a ruling and a rationale, then sends the exact inputs', async () => {
    const client = renderPanel([role('me', 'adjudicator')]);
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const user = userEvent.setup();
    const submit = await screen.findByRole('button', {
      name: 'Adjudicate Alpha trial',
    });

    expect(submit).toBeDisabled();
    await user.click(
      screen.getByRole('button', { name: 'Rule Exclude for Alpha trial' })
    );
    expect(submit).toBeDisabled();
    await user.type(
      screen.getByLabelText('Rationale for Alpha trial'),
      'protocol 3.2'
    );
    await user.click(submit);
    await user.click(submit);

    await waitFor(() => expect(adjudicateScreening).toHaveBeenCalledTimes(2));
    const [first, second] = vi.mocked(adjudicateScreening).mock.calls;
    expect(first.slice(0, 3)).toEqual(['project-1', 'queue-1', 'report-1']);
    expect(first[3]).toMatchObject({
      resolution_id: 'tip-1',
      input_observation_ids: ['obs-a', 'obs-b'],
      criteria_hash: HASH,
      decision: 'exclude',
      exclusion_reason: null,
      rationale: 'protocol 3.2',
    });
    expect(first[3].idempotency_key).toBeTruthy();
    expect(first[3].idempotency_key).not.toBe(second[3].idempotency_key);
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['screening-queues', 'project-1'],
      })
    );
  });

  it('a full-text exclusion ruling needs a protocol reason', async () => {
    vi.mocked(listScreeningQueues).mockResolvedValue([
      queue({ stage: 'full_text' }),
    ]);
    renderPanel([role('me', 'adjudicator')]);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', {
        name: 'Rule Exclude for Alpha trial',
      })
    );
    await user.type(screen.getByLabelText('Rationale for Alpha trial'), 'why');
    const submit = screen.getByRole('button', {
      name: 'Adjudicate Alpha trial',
    });

    expect(submit).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText('Exclusion reason for Alpha trial'),
      'wrong design'
    );
    await user.click(submit);

    await waitFor(() =>
      expect(vi.mocked(adjudicateScreening).mock.calls[0][3]).toMatchObject({
        exclusion_reason: 'wrong design',
      })
    );
  });

  it('a stale 409 shows the reload alert and refetches the conflicts', async () => {
    vi.mocked(adjudicateScreening).mockRejectedValue(
      apiError(409, 'Adjudication inputs are stale')
    );
    renderPanel([role('me', 'adjudicator')]);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', {
        name: 'Rule Include for Alpha trial',
      })
    );
    await user.type(screen.getByLabelText('Rationale for Alpha trial'), 'why');
    await user.click(
      screen.getByRole('button', { name: 'Adjudicate Alpha trial' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Inputs changed — reload conflicts'
    );
    await waitFor(() =>
      expect(
        vi.mocked(listScreeningConflicts).mock.calls.length
      ).toBeGreaterThan(1)
    );
  });

  it('a 403 shows the server reason', async () => {
    vi.mocked(adjudicateScreening).mockRejectedValue(
      apiError(403, 'Adjudicator reviewed this report')
    );
    renderPanel([role('me', 'adjudicator')]);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', {
        name: 'Rule Include for Alpha trial',
      })
    );
    await user.type(screen.getByLabelText('Rationale for Alpha trial'), 'why');
    await user.click(
      screen.getByRole('button', { name: 'Adjudicate Alpha trial' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Adjudicator reviewed this report'
    );
  });

  it('reopen needs a rationale and names the tip', async () => {
    renderPanel([role('me', 'adjudicator')]);
    const user = userEvent.setup();
    const reopen = await screen.findByRole('button', {
      name: 'Reopen Alpha trial',
    });

    expect(reopen).toBeDisabled();
    await user.type(
      screen.getByLabelText('Rationale for Alpha trial'),
      'new evidence'
    );
    await user.click(reopen);

    await waitFor(() => expect(reopenScreening).toHaveBeenCalled());
    const [projectId, queueId, reportId, body] =
      vi.mocked(reopenScreening).mock.calls[0];
    expect([projectId, queueId, reportId]).toEqual([
      'project-1',
      'queue-1',
      'report-1',
    ]);
    expect(body).toMatchObject({
      resolution_id: 'tip-1',
      rationale: 'new evidence',
    });
    expect(body.idempotency_key).toBeTruthy();
  });

  it('read-only (archived) shows conflicts but disables every action', async () => {
    renderPanel([role('me', 'adjudicator')], true);

    expect(await screen.findByText('Alpha trial')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Rule Include for Alpha trial' })
    ).toBeDisabled();
    expect(screen.getByLabelText('Rationale for Alpha trial')).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Adjudicate Alpha trial' })
    ).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Reopen Alpha trial' })
    ).toBeDisabled();
  });
});
