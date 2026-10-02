import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SynthesisPanel, reasonLabel } from '../SynthesisPanel';
import {
  executeSynthesis,
  listEvidence,
  listSynthesis,
  previewSynthesis,
  type EvidenceOutcomeList,
  type ProjectRoleAssignment,
  type SynthesisList,
  type SynthesisPreview,
  type SynthesisResult,
} from '@/services/researchEngineService';
import { listFormVersions } from '@/services/scispaceService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  listSynthesis: vi.fn(),
  listEvidence: vi.fn(),
  previewSynthesis: vi.fn(),
  executeSynthesis: vi.fn(),
  exportSynthesis: vi.fn(),
}));
vi.mock('@/services/scispaceService', () => ({
  listFormVersions: vi.fn(),
}));

const ROLES = ['mean_i', 'sd_i', 'n_i', 'mean_c', 'sd_c', 'n_c'] as const;
const LABELS = [
  'Intervention mean',
  'Intervention SD',
  'Intervention n',
  'Control mean',
  'Control SD',
  'Control n',
];
const selection = {
  measure: 'smd_hedges_g',
  model: 'random_effects_dl',
  outcome_key: 'depressive_symptoms',
  timepoint: '12 weeks',
};
const evidence = {
  outcomes: [
    {
      outcome_key: 'depressive_symptoms',
      timepoint: '12 weeks',
      tables: [
        {
          id: 'table-1',
          collection_id: 'project-1',
          protocol_version_id: 'version-1',
          outcome_key: 'depressive_symptoms',
          timepoint: '12 weeks',
          matrix_id: 'matrix-1',
          form_version_id: 'form-1',
          field_ids: ROLES.map((r) => `f-${r}`),
          rows: [],
          excluded: [],
          content_hash: 'a'.repeat(64),
          created_by_id: 'me',
          created_at: '2026-09-30T00:00:00Z',
          superseded: false,
          stale: false,
        },
      ],
      contradictions: [],
      certainty: [],
    },
  ],
} as unknown as EvidenceOutcomeList;

function result(overrides: Partial<SynthesisResult> = {}): SynthesisResult {
  return {
    id: 'result-1',
    collection_id: 'project-1',
    protocol_version_id: 'version-1',
    table_version_id: 'table-1',
    outcome_key: 'depressive_symptoms',
    timepoint: '12 weeks',
    measure: 'smd_hedges_g',
    model: 'random_effects_dl',
    config: {},
    config_hash: 'c'.repeat(64),
    estimator_version: 'nous.smd-hedges-g.dl/1',
    software: {},
    status: 'computed',
    included: [
      {
        unit: 'study:aaaaaaaa',
        report_ids: ['r1', 'r2'],
        accepted_value_ids: [],
        inputs: {
          mean_i: 4.1,
          sd_i: 2.6,
          n_i: 60,
          mean_c: 5.3,
          sd_c: 2.9,
          n_c: 60,
        },
        g: -0.4329406845,
        v: 0.0336910474,
        w_fixed: 29.68,
        w_random: 11.24,
      },
    ],
    excluded: [],
    estimate: -0.3084711364,
    se: 0.1551738785,
    ci_low: -0.6126063496,
    ci_high: -0.0043359232,
    q: 7.3067910677,
    df: 3,
    tau2: 0.0558459463,
    i2: 0.5894230487,
    input_hash: 'd'.repeat(64),
    result_hash: 'e'.repeat(64),
    executed_by_id: 'me',
    actor_role: 'reviewer',
    created_at: '2026-09-30T00:00:00Z',
    superseded: false,
    stale: false,
    ...overrides,
  };
}

function preview(overrides: Partial<SynthesisPreview> = {}): SynthesisPreview {
  return {
    selection,
    table_version_id: 'table-1',
    protocol_version_id: 'version-1',
    config: {},
    config_hash: 'c'.repeat(64),
    estimator_version: 'nous.smd-hedges-g.dl/1',
    included: [
      {
        unit: 'study:aaaaaaaa',
        report_ids: ['r1', 'r2'],
        accepted_value_ids: [],
        inputs: {
          mean_i: 4.1,
          sd_i: 2.6,
          n_i: 60,
          mean_c: 5.3,
          sd_c: 2.9,
          n_c: 60,
        },
      },
    ],
    excluded: [
      {
        unit: 'report:eeeeeeee',
        report_ids: ['r5'],
        reason: 'invalid_variance:c',
        detail: '0.0',
      },
    ],
    run_failures: [],
    input_hash: 'f'.repeat(64),
    ...overrides,
  };
}

const reviewer: ProjectRoleAssignment[] = [
  { user_id: 'me', role: 'reviewer' } as ProjectRoleAssignment,
];

function renderPanel(roles: ProjectRoleAssignment[] = reviewer): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <SynthesisPanel projectId="project-1" roles={roles} />
    </QueryClientProvider>
  );
}

async function mapRoles(): Promise<void> {
  const user = userEvent.setup();
  for (const [index, label] of LABELS.entries()) {
    const select = await screen.findByLabelText(label);
    await screen.findAllByRole('option', { name: ROLES[index] });
    await user.selectOptions(select, `f-${ROLES[index]}`);
  }
  await user.click(screen.getByRole('button', { name: 'Preview inputs' }));
}

describe('SynthesisPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listSynthesis).mockResolvedValue({
      selection,
      results: [],
    } as SynthesisList);
    vi.mocked(listEvidence).mockResolvedValue(evidence);
    vi.mocked(listFormVersions).mockResolvedValue([
      {
        id: 'form-1',
        version_no: 1,
        fields: ROLES.map((r) => ({ field_id: `f-${r}`, name: r })),
      },
    ] as never);
  });

  it('excluded reasons shown before execute', async () => {
    vi.mocked(previewSynthesis).mockResolvedValue(preview());
    renderPanel();
    await mapRoles();
    expect(
      await screen.findByText(/Invalid SD \(control\)/)
    ).toBeInTheDocument();
    expect(screen.getByText('2 reports')).toBeInTheDocument();
    expect(executeSynthesis).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Execute' })).toBeEnabled();
  });

  it('execute disabled on run failure', async () => {
    vi.mocked(previewSynthesis).mockResolvedValue(
      preview({
        run_failures: [
          {
            unit: null,
            report_ids: [],
            reason: 'unit_mismatch',
            detail: '%,GDS-15 points',
          },
        ],
      })
    );
    renderPanel();
    await mapRoles();
    expect(
      await screen.findByText(/Means and SDs do not share one unit/)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Execute' })).toBeDisabled();
  });

  it('stale banner', async () => {
    vi.mocked(listSynthesis).mockResolvedValue({
      selection,
      results: [result({ stale: true })],
    } as SynthesisList);
    renderPanel();
    expect(
      await screen.findByText(/Stale: its evidence table or protocol changed/)
    ).toBeInTheDocument();
  });

  it('I² shown as percent', async () => {
    vi.mocked(listSynthesis).mockResolvedValue({
      selection,
      results: [result()],
    } as SynthesisList);
    renderPanel();
    expect(await screen.findByText('58.9%')).toBeInTheDocument();
    expect(
      screen.getByText(/-0\.308 \(-0\.613 to -0\.004\)/)
    ).toBeInTheDocument();
  });

  it('labels every reason code', () => {
    expect(reasonLabel('missing_input:n_i')).toBe('Missing Intervention n');
    expect(reasonLabel('conflicting_reports:mean_c')).toBe(
      'Reports disagree on Control mean'
    );
    expect(reasonLabel('insufficient_studies')).toBe(
      'Fewer than two usable studies'
    );
  });
});
