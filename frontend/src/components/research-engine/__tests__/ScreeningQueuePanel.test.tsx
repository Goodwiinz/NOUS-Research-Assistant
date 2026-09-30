import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ScreeningQueuePanel } from '../ScreeningQueuePanel';
import { APIErrorClass } from '@/types/api';
import {
  assignScreeningReviewer,
  createScreeningQueue,
  getMyScreeningQueue,
  listFulltext,
  listScreeningHistory,
  listScreeningQueues,
  recordFulltextAttempt,
  requestFulltext,
  revokeScreeningAssignment,
  submitScreeningObservation,
  type FulltextState,
  type MyScreeningQueue,
  type ProjectRoleAssignment,
  type ScreeningQueue,
} from '@/services/researchEngineService';
import { projectService } from '@/services/projectService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  listScreeningQueues: vi.fn(),
  createScreeningQueue: vi.fn(),
  assignScreeningReviewer: vi.fn(),
  revokeScreeningAssignment: vi.fn(),
  getMyScreeningQueue: vi.fn(),
  submitScreeningObservation: vi.fn(),
  listScreeningHistory: vi.fn(),
  listFulltext: vi.fn(),
  requestFulltext: vi.fn(),
  recordFulltextAttempt: vi.fn(),
}));
vi.mock('@/services/projectService', () => ({
  projectService: { listProjectDocuments: vi.fn() },
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
  assignment_count: 1,
  observation_count: 0,
  suggestion_count: 0,
  ...overrides,
});

const mine = (
  overrides: Partial<MyScreeningQueue['queue']> = {},
  observed = false
): MyScreeningQueue => ({
  queue: {
    id: 'queue-1',
    stage: 'title_abstract',
    protocol_version_id: 'version-1',
    criteria_hash: HASH,
    exclusion_reasons: ['wrong population', 'wrong design'],
    ...overrides,
  },
  assignment_id: 'assignment-1',
  items: [
    {
      report_id: 'report-1',
      title_snapshot: 'Alpha trial',
      identifiers: { doi: ['10.1000/alpha'] },
      abstract: 'Adults with a condition.',
      reveal_state: 'hidden',
      observation: observed
        ? {
            id: 'obs-1',
            queue_id: 'queue-1',
            report_id: 'report-1',
            reviewer_id: 'me',
            assignment_id: 'assignment-1',
            decision: 'include',
            created_at: '2026-09-29T00:00:00Z',
          }
        : null,
    },
    {
      report_id: 'report-2',
      title_snapshot: 'Beta cohort',
      identifiers: {},
      abstract: null,
      reveal_state: 'hidden',
    },
  ],
  counts: { total: 2, screened: observed ? 1 : 0, remaining: observed ? 1 : 2 },
});

const fulltextState = (
  state: FulltextState['state'],
  overrides: Partial<FulltextState> = {}
): FulltextState => ({
  request_id: 'request-1',
  report_id: 'report-1',
  requested_by_id: 'me',
  requested_at: '2026-09-29T00:00:00Z',
  state,
  head_attempt_id: state === 'pending' ? null : 'attempt-1',
  attempts:
    state === 'pending'
      ? []
      : [
          {
            id: 'attempt-1',
            outcome: state,
            reason: state === 'unavailable' ? 'not held by library' : null,
            attempted_on: '2026-09-28',
            actor_id: 'me',
            document_available: state === 'retrieved',
            created_at: '2026-09-29T00:00:00Z',
          },
        ],
  ...overrides,
});

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
  props: { readOnly?: boolean; approvedProtocolVersionId?: string } = {}
): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ScreeningQueuePanel projectId="project-1" roles={roles} {...props} />
    </QueryClientProvider>
  );
  return client;
}

describe('ScreeningQueuePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listScreeningQueues).mockResolvedValue([queue()]);
    vi.mocked(getMyScreeningQueue).mockResolvedValue(mine());
    vi.mocked(listScreeningHistory).mockResolvedValue([]);
    vi.mocked(listFulltext).mockResolvedValue([]);
    vi.mocked(submitScreeningObservation).mockResolvedValue(
      {} as Awaited<ReturnType<typeof submitScreeningObservation>>
    );
  });

  it('renders my queue rows with abstract, identifiers and counts', async () => {
    renderPanel([role('me', 'reviewer')]);

    expect(await screen.findByText('Alpha trial')).toBeInTheDocument();
    expect(screen.getByText('Adults with a condition.')).toBeInTheDocument();
    expect(screen.getByText('doi: 10.1000/alpha')).toBeInTheDocument();
    expect(screen.getByText('0 / 2 screened')).toBeInTheDocument();
    expect(getMyScreeningQueue).toHaveBeenCalledWith('project-1', 'queue-1');
  });

  it('submits with the queue criteria and assignment, then invalidates', async () => {
    const client = renderPanel([role('me', 'reviewer')]);
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const user = userEvent.setup();

    await user.type(
      await screen.findByLabelText('Note for Alpha trial'),
      'fits population'
    );
    await user.click(
      screen.getByRole('button', { name: 'Include Alpha trial' })
    );

    await waitFor(() => expect(submitScreeningObservation).toHaveBeenCalled());
    const [projectId, queueId, body] = vi.mocked(submitScreeningObservation)
      .mock.calls[0];
    expect([projectId, queueId]).toEqual(['project-1', 'queue-1']);
    expect(body).toMatchObject({
      report_id: 'report-1',
      assignment_id: 'assignment-1',
      criteria_hash: HASH,
      decision: 'include',
      exclusion_reason: null,
      note: 'fits population',
      supersedes_observation_id: null,
    });
    expect(body.idempotency_key).toBeTruthy();
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['screening-queues', 'project-1'],
      })
    );
  });

  it('blocks a full-text exclusion until a protocol reason is chosen', async () => {
    vi.mocked(listScreeningQueues).mockResolvedValue([
      queue({ stage: 'full_text' }),
    ]);
    vi.mocked(getMyScreeningQueue).mockResolvedValue(
      mine({ stage: 'full_text' })
    );
    vi.mocked(listFulltext).mockResolvedValue([fulltextState('retrieved')]);
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();

    expect(
      await screen.findByLabelText('Full-text status for Alpha trial')
    ).toHaveTextContent('Full text: retrieved');
    const exclude = screen.getByRole('button', {
      name: 'Exclude Alpha trial',
    });
    expect(exclude).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText('Exclusion reason for Alpha trial'),
      'wrong design'
    );
    expect(exclude).toBeEnabled();
    await user.click(exclude);

    await waitFor(() =>
      expect(
        vi.mocked(submitScreeningObservation).mock.calls[0][2]
      ).toMatchObject({ decision: 'exclude', exclusion_reason: 'wrong design' })
    );
  });

  it('shows a full-text badge per state, with the unavailable reason', async () => {
    const other = { request_id: 'request-2', report_id: 'report-2' };
    vi.mocked(listFulltext).mockResolvedValue([
      fulltextState('unavailable'),
      fulltextState('pending', other),
    ]);
    renderPanel([role('me', 'reviewer')]);

    const alpha = await screen.findByLabelText(
      'Full-text status for Alpha trial'
    );
    await waitFor(() =>
      expect(alpha).toHaveTextContent('Full text: unavailable')
    );
    expect(alpha).toHaveAttribute('title', 'not held by library');
    expect(
      screen.getByLabelText('Full-text status for Beta cohort')
    ).toHaveTextContent('Full text: pending');
  });

  it('requests full text for a row with one key per click', async () => {
    vi.mocked(requestFulltext).mockResolvedValue(fulltextState('pending'));
    const client = renderPanel([role('me', 'reviewer')]);
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', {
        name: 'Request full text for Alpha trial',
      })
    );

    await waitFor(() => expect(requestFulltext).toHaveBeenCalled());
    const [projectId, body] = vi.mocked(requestFulltext).mock.calls[0];
    expect(projectId).toBe('project-1');
    expect(body.report_id).toBe('report-1');
    expect(body.idempotency_key).toBeTruthy();
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ['prisma', 'project-1'],
      })
    );
  });

  it('marking unavailable requires a reason and names the head', async () => {
    vi.mocked(listFulltext).mockResolvedValue([fulltextState('requested')]);
    vi.mocked(recordFulltextAttempt).mockResolvedValue(
      fulltextState('unavailable')
    );
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', {
        name: 'Mark full text unavailable for Alpha trial',
      })
    );
    const save = screen.getByRole('button', {
      name: 'Save full-text attempt for Alpha trial',
    });
    expect(save).toBeDisabled();
    await user.type(
      screen.getByLabelText('Reason full text is unavailable for Alpha trial'),
      'embargoed'
    );
    await user.clear(screen.getByLabelText('Date attempted for Alpha trial'));
    await user.type(
      screen.getByLabelText('Date attempted for Alpha trial'),
      '2026-09-20'
    );
    await user.click(save);

    await waitFor(() => expect(recordFulltextAttempt).toHaveBeenCalled());
    const [, requestId, body] = vi.mocked(recordFulltextAttempt).mock.calls[0];
    expect(requestId).toBe('request-1');
    expect(body).toMatchObject({
      outcome: 'unavailable',
      reason: 'embargoed',
      attempted_on: '2026-09-20',
      document_id: null,
      previous_attempt_id: 'attempt-1',
    });
  });

  it('marking retrieved offers only the project documents', async () => {
    vi.mocked(listFulltext).mockResolvedValue([fulltextState('pending')]);
    vi.mocked(projectService.listProjectDocuments).mockResolvedValue({
      documents: [
        {
          id: 'link-1',
          project_id: 'project-1',
          document_id: 'doc-1',
          document: {
            id: 'doc-1',
            title: 'Alpha full text',
            filename: 'alpha.pdf',
            status: 'completed',
          },
        },
      ],
      total: 1,
    } as Awaited<ReturnType<typeof projectService.listProjectDocuments>>);
    vi.mocked(recordFulltextAttempt).mockResolvedValue(
      fulltextState('retrieved')
    );
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', {
        name: 'Mark full text retrieved for Alpha trial',
      })
    );
    const select = screen.getByLabelText(
      'Project document with the full text of Alpha trial'
    );
    await screen.findByRole('option', { name: 'Alpha full text' });
    expect(
      Array.from(select.querySelectorAll('option')).map((o) => o.value)
    ).toEqual(['', 'doc-1']);
    expect(projectService.listProjectDocuments).toHaveBeenCalledWith(
      'project-1',
      { limit: 100 }
    );
    await user.selectOptions(select, 'doc-1');
    await user.click(
      screen.getByRole('button', {
        name: 'Save full-text attempt for Alpha trial',
      })
    );

    await waitFor(() =>
      expect(vi.mocked(recordFulltextAttempt).mock.calls[0][2]).toMatchObject({
        outcome: 'retrieved',
        document_id: 'doc-1',
        reason: null,
        previous_attempt_id: null,
      })
    );
  });

  it('a full-text decision stays disabled until the full text is retrieved', async () => {
    vi.mocked(listScreeningQueues).mockResolvedValue([
      queue({ stage: 'full_text' }),
    ]);
    vi.mocked(getMyScreeningQueue).mockResolvedValue(
      mine({ stage: 'full_text' })
    );
    vi.mocked(listFulltext).mockResolvedValue([fulltextState('unavailable')]);
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();

    await screen.findByText('Alpha trial');
    await waitFor(() => expect(listFulltext).toHaveBeenCalled());
    await user.selectOptions(
      screen.getByLabelText('Exclusion reason for Alpha trial'),
      'wrong design'
    );
    for (const name of ['Include', 'Exclude', 'Uncertain']) {
      expect(
        screen.getByRole('button', { name: `${name} Alpha trial` })
      ).toBeDisabled();
    }
    expect(screen.getAllByText('Full text not retrieved').length).toBe(2);
  });

  it('read-only hides full-text actions', async () => {
    vi.mocked(listFulltext).mockResolvedValue([fulltextState('requested')]);
    renderPanel([role('me', 'reviewer')], { readOnly: true });

    expect(
      await screen.findByLabelText('Full-text status for Alpha trial')
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /full text/i })
    ).not.toBeInTheDocument();
  });

  it('changing a decision supersedes the current observation', async () => {
    vi.mocked(getMyScreeningQueue).mockResolvedValue(mine({}, true));
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();

    expect(await screen.findByText('1 / 2 screened')).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', { name: 'Change to Uncertain Alpha trial' })
    );

    await waitFor(() =>
      expect(
        vi.mocked(submitScreeningObservation).mock.calls[0][2]
      ).toMatchObject({
        decision: 'uncertain',
        supersedes_observation_id: 'obs-1',
      })
    );
  });

  it('a stale queue shows the banner and disables every decision', async () => {
    vi.mocked(getMyScreeningQueue).mockResolvedValue(
      mine({ stale: 'Protocol version changed; reconcile queue' })
    );
    renderPanel([role('me', 'reviewer')]);

    expect(
      await screen.findByText(
        'This queue is stale: Protocol version changed; reconcile queue. A supervisor must reconcile it.'
      )
    ).toBeInTheDocument();
    for (const name of ['Include', 'Exclude', 'Uncertain']) {
      expect(
        screen.getByRole('button', { name: `${name} Alpha trial` })
      ).toBeDisabled();
    }
  });

  it('read-only (archived) disables decisions and hides supervision', async () => {
    renderPanel([role('me', 'reviewer'), role('me', 'supervisor')], {
      readOnly: true,
      approvedProtocolVersionId: 'version-1',
    });

    expect(
      await screen.findByRole('button', { name: 'Include Alpha trial' })
    ).toBeDisabled();
    expect(
      screen.queryByRole('button', { name: 'Create title/abstract queue' })
    ).not.toBeInTheDocument();
  });

  it('shows supervisor controls only to a supervisor', async () => {
    renderPanel([role('me', 'reviewer'), role('other', 'supervisor')], {
      approvedProtocolVersionId: 'version-1',
    });

    expect(await screen.findByText('Alpha trial')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Create title/abstract queue' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText('Reviewer to assign')
    ).not.toBeInTheDocument();
  });

  it('a supervisor creates, assigns and revokes (revoke needs a reason)', async () => {
    vi.mocked(listScreeningHistory).mockResolvedValue([
      {
        seq: 2,
        event_type: 'screening.assigned',
        actor_user_id: 'me',
        actor_role: 'supervisor',
        payload: { assignment_id: 'assignment-9', reviewer_id: 'rev-1' },
        occurred_at: '2026-09-29T00:00:00Z',
      },
    ]);
    vi.mocked(createScreeningQueue).mockResolvedValue(queue({ id: 'queue-2' }));
    vi.mocked(assignScreeningReviewer).mockResolvedValue(
      {} as Awaited<ReturnType<typeof assignScreeningReviewer>>
    );
    vi.mocked(revokeScreeningAssignment).mockResolvedValue(
      {} as Awaited<ReturnType<typeof revokeScreeningAssignment>>
    );
    renderPanel([role('me', 'supervisor'), role('rev-1', 'reviewer')], {
      approvedProtocolVersionId: 'version-1',
    });
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: 'Create title/abstract queue' })
    );
    await waitFor(() =>
      expect(vi.mocked(createScreeningQueue).mock.calls[0][1]).toMatchObject({
        protocol_version_id: 'version-1',
        stage: 'title_abstract',
      })
    );

    await user.selectOptions(
      screen.getByLabelText('Reviewer to assign'),
      'rev-1'
    );
    await user.click(screen.getByRole('button', { name: 'Assign' }));
    await waitFor(() =>
      expect(vi.mocked(assignScreeningReviewer).mock.calls[0][2]).toMatchObject(
        {
          reviewer_user_id: 'rev-1',
        }
      )
    );

    const revoke = await screen.findByRole('button', {
      name: 'Revoke assignment of rev-1',
    });
    expect(revoke).toBeDisabled();
    await user.type(
      screen.getByLabelText('Reason for revoking an assignment'),
      'left the team'
    );
    await user.click(revoke);
    await waitFor(() =>
      expect(vi.mocked(revokeScreeningAssignment).mock.calls[0]).toMatchObject([
        'project-1',
        expect.any(String),
        'assignment-9',
        { reason: 'left the team' },
      ])
    );
  });

  it('surfaces an API error as an alert', async () => {
    vi.mocked(submitScreeningObservation).mockRejectedValue(
      new Error('Observation exists; supersede the current observation')
    );
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: 'Include Alpha trial' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Observation exists; supersede the current observation'
    );
  });

  it('never fetches ledger history for a reviewer', async () => {
    renderPanel([role('me', 'reviewer')]);

    expect(await screen.findByText('Alpha trial')).toBeInTheDocument();
    expect(listScreeningHistory).not.toHaveBeenCalled();
  });

  it('two clicks send two different idempotency keys', async () => {
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();
    const include = await screen.findByRole('button', {
      name: 'Include Alpha trial',
    });

    await user.click(include);
    await waitFor(() =>
      expect(submitScreeningObservation).toHaveBeenCalledTimes(1)
    );
    await user.click(
      await screen.findByRole('button', { name: 'Include Alpha trial' })
    );
    await waitFor(() =>
      expect(submitScreeningObservation).toHaveBeenCalledTimes(2)
    );

    const [first, second] = vi
      .mocked(submitScreeningObservation)
      .mock.calls.map(([, , body]) => body.idempotency_key);
    expect(first).toBeTruthy();
    expect(first).not.toBe(second);
  });

  it('clears the row note after a successful submission', async () => {
    renderPanel([role('me', 'reviewer')]);
    const user = userEvent.setup();
    const note = await screen.findByLabelText('Note for Alpha trial');

    await user.type(note, 'fits population');
    await user.click(
      screen.getByRole('button', { name: 'Include Alpha trial' })
    );

    await waitFor(() =>
      expect(screen.getByLabelText('Note for Alpha trial')).toHaveValue('')
    );
  });

  it('surfaces a history failure to a supervisor', async () => {
    vi.mocked(listScreeningHistory).mockRejectedValue(new Error('ledger down'));
    renderPanel([role('me', 'supervisor')], {
      approvedProtocolVersionId: 'version-1',
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('ledger down');
  });

  it('an unassigned reviewer sees a notice, other failures an alert', async () => {
    vi.mocked(getMyScreeningQueue).mockRejectedValue(
      new APIErrorClass({
        message: 'Not assigned to this queue',
        status_code: 403,
        type: 'http_error',
      })
    );
    renderPanel([role('me', 'reviewer')]);

    expect(
      await screen.findByText('You are not assigned to this queue.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a failed queue load is an alert', async () => {
    vi.mocked(getMyScreeningQueue).mockRejectedValue(
      new APIErrorClass({
        message: 'backend down',
        status_code: 500,
        type: 'http_error',
      })
    );
    renderPanel([role('me', 'reviewer')]);

    expect(await screen.findByRole('alert')).toHaveTextContent('backend down');
  });

  it('a hidden row says so and shows no peer decision', async () => {
    renderPanel([role('me', 'reviewer')]);

    expect(await screen.findByText('Alpha trial')).toBeInTheDocument();
    expect(
      screen.getAllByText("Other reviewers' decisions are hidden until reveal.")
    ).toHaveLength(2);
    expect(screen.queryByText(/Reviewer peer/)).not.toBeInTheDocument();
  });

  it('a revealed row shows peers and the badge, and locks until reopened', async () => {
    const view = mine({}, true);
    view.items[0] = {
      ...view.items[0],
      reveal_state: 'revealed',
      others: [
        {
          id: 'obs-peer',
          queue_id: 'queue-1',
          report_id: 'report-1',
          reviewer_id: 'peer',
          assignment_id: 'assignment-2',
          decision: 'exclude',
          created_at: '2026-09-29T00:00:00Z',
        },
      ],
      resolution: {
        id: 'res-1',
        report_id: 'report-1',
        basis: 'conflict',
        input_observation_ids: ['obs-1', 'obs-peer'],
        criteria_hash: HASH,
        created_at: '2026-09-29T00:00:00Z',
      },
    };
    vi.mocked(getMyScreeningQueue).mockResolvedValue(view);
    renderPanel([role('me', 'reviewer')]);

    expect(await screen.findByText('Conflict')).toBeInTheDocument();
    expect(screen.getByText('Reviewer peer: exclude')).toBeInTheDocument();
    const change = screen.getByRole('button', {
      name: 'Change to Exclude Alpha trial',
    });
    expect(change).toBeDisabled();
    expect(change).toHaveAttribute(
      'title',
      'Resolved — an adjudicator must reopen'
    );
    // The unrevealed row stays open.
    expect(
      screen.getByRole('button', { name: 'Include Beta cohort' })
    ).toBeEnabled();
  });

  it.each([
    ['agreement', 'include', null, 'Agreed: include'],
    [
      'adjudicated',
      'exclude',
      'wrong design',
      'Adjudicated: exclude — wrong design',
    ],
    ['reopened', null, null, 'Reopened'],
  ] as const)(
    'labels a %s resolution',
    async (basis, outcome, reason, label) => {
      const view = mine();
      view.items[0] = {
        ...view.items[0],
        reveal_state: 'revealed',
        resolution: {
          id: 'res-1',
          report_id: 'report-1',
          basis,
          outcome,
          exclusion_reason: reason,
          input_observation_ids: [],
          criteria_hash: HASH,
          created_at: '2026-09-29T00:00:00Z',
        },
      };
      vi.mocked(getMyScreeningQueue).mockResolvedValue(view);
      renderPanel([role('me', 'reviewer')]);

      expect(await screen.findByText(label)).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Include Alpha trial' })
      ).toHaveProperty('disabled', basis !== 'reopened');
    }
  );
});
