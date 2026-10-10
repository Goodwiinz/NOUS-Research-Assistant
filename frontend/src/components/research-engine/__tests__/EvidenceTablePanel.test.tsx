import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EvidenceTablePanel, deriveLevel } from '../EvidenceTablePanel';
import {
  listAppraisals,
  listEvidence,
  previewEvidenceTable,
  type Contradiction,
  type EvidenceOutcomeList,
  type EvidenceTable,
  type EvidenceTablePreview,
  type ProjectRoleAssignment,
} from '@/services/researchEngineService';
import { listFormVersions, listMatrices } from '@/services/scispaceService';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me' } }),
}));
vi.mock('@/services/researchEngineService', () => ({
  listEvidence: vi.fn(),
  previewEvidenceTable: vi.fn(),
  createEvidenceTable: vi.fn(),
  recordContradiction: vi.fn(),
  assessCertainty: vi.fn(),
  exportEvidence: vi.fn(),
  listAppraisals: vi.fn(),
}));
vi.mock('@/services/scispaceService', () => ({
  listMatrices: vi.fn(),
  listFormVersions: vi.fn(),
}));

const FIELD = 'field-gds';
const rows: EvidenceTablePreview['rows'] = [
  {
    row_key: 'report:r3|depressive_symptoms|12 weeks',
    unit: 'report:r3',
    report_ids: ['r3'],
    cells: { [FIELD]: { state: 'missing', value: null, tips: [] } },
  },
  {
    row_key: 'study:s|depressive_symptoms|12 weeks',
    unit: 'study:s',
    report_ids: ['r1', 'r2'],
    cells: {
      [FIELD]: {
        state: 'conflict',
        value: null,
        tips: [
          {
            accepted_value_id: 'av-1',
            document_id: 'doc-1',
            report_id: 'r1',
            source_hash: 'a'.repeat(64),
            value: 4.1,
          },
          {
            accepted_value_id: 'av-2',
            document_id: 'doc-2',
            report_id: 'r2',
            source_hash: 'b'.repeat(64),
            value: 4.4,
          },
        ],
      },
    },
  },
];

const table: EvidenceTable = {
  id: 'table-1',
  collection_id: 'project-1',
  protocol_version_id: 'version-1',
  outcome_key: 'depressive_symptoms',
  timepoint: '12 weeks',
  matrix_id: 'matrix-1',
  form_version_id: 'form-1',
  field_ids: [FIELD],
  rows,
  excluded: [],
  content_hash: 'c'.repeat(64),
  created_by_id: 'me',
  created_at: '2026-09-30T00:00:00Z',
  superseded: false,
  stale: false,
};

const group: Contradiction = {
  contradiction_id: 'group-1',
  table_version_id: 'table-1',
  field_id: FIELD,
  status: 'unresolved',
  rows: [
    {
      id: 'group-1',
      collection_id: 'project-1',
      contradiction_id: 'group-1',
      table_version_id: 'table-1',
      field_id: FIELD,
      accepted_value_ids: ['av-1', 'av-2'],
      kind: 'opened',
      explanation: 'two reports disagree',
      actor_id: 'me',
      actor_role: 'reviewer',
      created_at: '2026-09-30T00:00:00Z',
    },
  ],
  dissent: [],
  stale: false,
};

const listing: EvidenceOutcomeList = {
  protocol_version_id: 'version-1',
  certainty_method: {
    method: 'grade',
    version: 'handbook-2013',
    domains: [],
    levels: [],
    starting_levels: [],
  },
  outcomes: [
    {
      outcome_key: 'depressive_symptoms',
      timepoint: '12 weeks',
      tables: [table],
      contradictions: [group],
      certainty: [],
    },
  ],
};

const preview: EvidenceTablePreview = {
  protocol_version_id: 'version-1',
  outcome_key: 'depressive_symptoms',
  timepoint: '12 weeks',
  matrix_id: 'matrix-1',
  form_version_id: 'form-1',
  field_ids: [FIELD],
  rows,
  excluded: [],
  content_hash: 'c'.repeat(64),
  tip_id: 'table-1',
  differs_from_tip: false,
  unreviewed_cells: [
    {
      document_id: 'doc-3',
      field_id: FIELD,
      column_name: 'GDS mean',
      value: 3.9,
      source: 'machine',
      review_state: 'unreviewed',
    },
  ],
  stance_suggestions: [
    {
      claim_hash: 'c'.repeat(64),
      claim_text: 'Exercise reduces depressive symptoms',
      review_state: 'unreviewed_model_suggestion',
      suggestions: [
        {
          id: 'stance-1',
          source_id: 'doc-1',
          stance: 'supporting',
          confidence: 0.8,
          model_version: 'meter-1',
        },
        {
          id: 'stance-2',
          source_id: 'doc-3',
          stance: 'opposing',
          confidence: 0.7,
          model_version: 'meter-1',
        },
      ],
    },
  ],
};

function role(r: 'reviewer' | 'adjudicator'): ProjectRoleAssignment {
  return {
    id: `role-${r}`,
    collection_id: 'project-1',
    user_id: 'me',
    role: r,
    assigned_by_id: 'owner',
    created_at: '2026-09-30T00:00:00Z',
  } as ProjectRoleAssignment;
}

function renderPanel(roles: ProjectRoleAssignment[]): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EvidenceTablePanel projectId="project-1" roles={roles} />
    </QueryClientProvider>
  );
}

describe('EvidenceTablePanel', () => {
  beforeEach(() => {
    vi.mocked(listEvidence).mockResolvedValue(listing);
    vi.mocked(previewEvidenceTable).mockResolvedValue(preview);
    vi.mocked(listAppraisals).mockResolvedValue({ results: [] });
    vi.mocked(listMatrices).mockResolvedValue({
      matrices: [
        {
          id: 'matrix-1',
          project_id: 'project-1',
          name: 'Outcomes',
          columns: [],
          created_at: '2026-09-30T00:00:00Z',
        },
      ],
      total: 1,
    });
    vi.mocked(listFormVersions).mockResolvedValue([
      {
        id: 'form-1',
        matrix_id: 'matrix-1',
        version_no: 1,
        provenance: 'authored',
        content_hash: 'd'.repeat(64),
        created_at: '2026-09-30T00:00:00Z',
        fields: [{ field_id: FIELD, name: 'GDS mean', timepoint: '12 weeks' }],
      },
    ]);
  });

  it('missing cell never shows agreement', async () => {
    renderPanel([role('reviewer')]);
    const missing = await screen.findByLabelText(
      'Report r3 GDS mean: Not reported / missing'
    );
    expect(missing).toHaveTextContent('Not reported / missing');
    expect(
      screen.getByLabelText('Study s GDS mean: Reports disagree')
    ).toBeInTheDocument();
    expect(previewEvidenceTable).toHaveBeenCalledWith('project-1', {
      outcome_key: 'depressive_symptoms',
      timepoint: '12 weeks',
      matrix_id: 'matrix-1',
      field_ids: [FIELD],
    });
    // Machine cells sit apart, collapsed and labelled unreviewed.
    expect(screen.getByText('Unreviewed (machine/legacy) · 1')).toBeVisible();
  });

  it('suggestion labelled unreviewed', async () => {
    renderPanel([role('reviewer')]);
    const label = await screen.findByText('Model suggestion — unreviewed');
    const card = label.parentElement as HTMLElement;
    expect(within(card).getByText(/supporting/)).toBeInTheDocument();
    expect(within(card).queryByText(/0\.8/)).not.toBeInTheDocument();
    expect(
      within(card).getByRole('button', { name: 'Open contradiction' })
    ).toBeInTheDocument();
  });

  it('resolve hidden without adjudicator', async () => {
    renderPanel([role('reviewer')]);
    expect(await screen.findByText('Unresolved')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Add dissent' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Resolve' })
    ).not.toBeInTheDocument();
  });

  it('shows resolve to an adjudicator', async () => {
    renderPanel([role('adjudicator')]);
    expect(
      await screen.findByRole('button', { name: 'Resolve' })
    ).toBeInTheDocument();
  });

  it('level derived and null when unknown', async () => {
    const user = userEvent.setup();
    renderPanel([role('reviewer')]);
    const derived = await screen.findByText(/Derived level/);
    expect(derived).toHaveTextContent('Derived level: Unknown');
    for (const [label, value] of [
      ['Risk of bias', '-1'],
      ['Inconsistency', '0'],
      ['Indirectness', '0'],
      ['Imprecision', '0'],
    ] as const) {
      await user.selectOptions(screen.getByLabelText(label), value);
    }
    expect(derived).toHaveTextContent('Derived level: Unknown');
    await user.selectOptions(screen.getByLabelText('Publication bias'), '0');
    expect(derived).toHaveTextContent('Derived level: Moderate');
    expect(derived).toHaveTextContent('1 unresolved contradiction(s)');
    expect(
      deriveLevel('high', {
        risk_of_bias: -2,
        inconsistency: -2,
        indirectness: 0,
        imprecision: 0,
        publication_bias: 0,
      })
    ).toBe('very_low');
  });
});
