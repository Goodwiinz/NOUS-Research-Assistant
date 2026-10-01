import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AppraisalPanel } from '../AppraisalPanel';
import {
  adjudicateAppraisal,
  listAppraisals,
  submitAppraisal,
  type Appraisal,
  type AppraisalInstrument,
  type AppraisalList,
  type AppraisalResult,
  type ProjectRoleAssignment,
} from '@/services/researchEngineService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  listAppraisals: vi.fn(),
  submitAppraisal: vi.fn(),
  adjudicateAppraisal: vi.fn(),
  exportAppraisals: vi.fn(),
}));

const DOMAINS = [
  { id: 'D1', name: 'Randomization process', signals: ['1.1', '1.2', '1.3'] },
  {
    id: 'D2',
    name: 'Deviations from intended interventions',
    signals: ['2.1'],
  },
  { id: 'D3', name: 'Missing outcome data', signals: ['3.1'] },
  { id: 'D4', name: 'Measurement of the outcome', signals: ['4.1'] },
  { id: 'D5', name: 'Selection of the reported result', signals: ['5.1'] },
];

const instrument: AppraisalInstrument = {
  key: 'rob2',
  version: '2019-08-22',
  spec_hash: 'f'.repeat(64),
  mode: 'dual_independent',
  variant: 'individually_randomized_parallel_group/assignment',
  domains: DOMAINS,
  responses: ['Y', 'PY', 'PN', 'N', 'NI', 'NA'],
  judgments: ['low', 'some_concerns', 'high'],
  designs: ['randomized_parallel_group', 'cohort'],
  applies_to: ['randomized_parallel_group'],
  licence: 'CC BY-NC-ND 4.0 (riskofbias.info)',
  encoding: 'structure only; no text or algorithms',
  source: 'https://www.riskofbias.info',
};

const row = (id: string, assessor: string): Appraisal => ({
  id,
  collection_id: 'project-1',
  protocol_version_id: 'version-1',
  instrument_key: 'rob2',
  instrument_version: '2019-08-22',
  instrument_spec_hash: 'f'.repeat(64),
  study_id: 'study-1',
  report_id: null,
  target_key: 'study:study-1',
  outcome_key: 'depressive_symptoms',
  timepoint: '12 weeks',
  study_design: 'randomized_parallel_group',
  applicability: 'applicable',
  domains: {
    D3: { judgment: null, signals: {}, rationale: null, evidence: [] },
    D4: {
      judgment: 'high',
      signals: {},
      rationale: 'assessors aware',
      evidence: [],
    },
  },
  overall: null,
  kind: 'independent',
  actor_role: 'reviewer',
  assessor_id: assessor,
  assessor_name: `Reviewer ${assessor}`,
  resolves_assessment_ids: null,
  rationale: null,
  input_hash: 'a'.repeat(64),
  supersedes_assessment_id: null,
  created_at: '2026-09-30T00:00:00Z',
  superseded: false,
  stale: false,
});

const result = (overrides: Partial<AppraisalResult> = {}): AppraisalResult => ({
  target_key: 'study:study-1',
  study_id: 'study-1',
  report_id: null,
  outcome_key: 'depressive_symptoms',
  timepoint: '12 weeks',
  status: 'awaiting_independent',
  unresolved_domains: [],
  stale: false,
  mine: false,
  rows: [],
  evidence_options: [],
  ...overrides,
});

const listing = (results: AppraisalResult[]): AppraisalList => ({
  protocol_version_id: 'version-1',
  instrument,
  outcomes: { depressive_symptoms: ['12 weeks'] },
  results,
});

const role = (name: ProjectRoleAssignment['role']): ProjectRoleAssignment => ({
  id: `me-${name}`,
  project_id: 'project-1',
  user_id: 'me',
  role: name,
  assigned_by_id: 'owner',
  created_at: '2026-09-30T00:00:00Z',
});

function renderPanel(roles: ProjectRoleAssignment[]): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AppraisalPanel projectId="project-1" roles={roles} />
    </QueryClientProvider>
  );
}

describe('AppraisalPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(submitAppraisal).mockResolvedValue(row('new', 'me'));
    vi.mocked(adjudicateAppraisal).mockResolvedValue(row('adj', 'me'));
  });

  it('shows awaiting without peer details', async () => {
    vi.mocked(listAppraisals).mockResolvedValue(listing([result()]));
    renderPanel([role('reviewer')]);
    expect(
      await screen.findByLabelText('Status: Awaiting independent assessments')
    ).toHaveTextContent('Awaiting');
    expect(screen.queryByText(/Reviewer /)).not.toBeInTheDocument();
    expect(screen.queryByText(/submitted/i)).not.toBeInTheDocument();
    expect(screen.getByText(/riskofbias\.info/)).toBeInTheDocument();
  });

  it('unknown judgement posts null', async () => {
    vi.mocked(listAppraisals).mockResolvedValue(listing([result()]));
    renderPanel([role('reviewer')]);
    const judgement = await screen.findByLabelText(
      'Randomization process judgement'
    );
    expect(judgement).toHaveDisplayValue('Unknown');
    await userEvent.click(
      screen.getByRole('button', { name: 'Submit appraisal' })
    );
    await waitFor(() => expect(submitAppraisal).toHaveBeenCalledTimes(1));
    const [projectId, body] = vi.mocked(submitAppraisal).mock.calls[0];
    expect(projectId).toBe('project-1');
    expect(body.domains?.D1).toEqual({
      judgment: null,
      signals: { '1.1': null, '1.2': null, '1.3': null },
      rationale: null,
      evidence: [],
    });
    expect(body.overall).toBeNull();
    expect(body.study_id).toBe('study-1');
    expect(body.supersedes_assessment_id).toBeNull();
    expect(body.idempotency_key).toEqual(expect.any(String));
  });

  it('adjudicate hidden without role', async () => {
    const conflict = result({
      status: 'conflict',
      unresolved_domains: ['D4'],
      rows: [row('a', 'r1'), row('b', 'r2')],
    });
    vi.mocked(listAppraisals).mockResolvedValue(listing([conflict]));
    renderPanel([role('reviewer')]);
    await screen.findByLabelText(
      'Status: Assessors disagree; needs an adjudicator'
    );
    expect(
      screen.queryByRole('button', { name: 'Record adjudication' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Submit appraisal' })
    ).not.toBeInTheDocument();
  });

  it('lists unresolved domains', async () => {
    const conflict = result({
      status: 'conflict',
      unresolved_domains: ['D3', 'D4'],
      rows: [row('a', 'r1'), row('b', 'r2')],
    });
    vi.mocked(listAppraisals).mockResolvedValue(listing([conflict]));
    renderPanel([role('adjudicator')]);
    expect(
      await screen.findByText(
        'Unresolved: Missing outcome data, Measurement of the outcome'
      )
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByLabelText('Adjudication rationale'),
      'Checked the full text.'
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Record adjudication' })
    );
    await waitFor(() => expect(adjudicateAppraisal).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(adjudicateAppraisal).mock.calls[0];
    expect(body.resolves_assessment_ids).toEqual(['a', 'b']);
    expect(body.rationale).toBe('Checked the full text.');
  });
});
