import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/test-utils';
import { ExtractionMatrix } from '../ExtractionMatrix';
import { CellCitation } from '../CellCitation';
import { CellObservations } from '../CellObservations';
import {
  getCellObservations,
  getMatrix,
  listFormVersions,
} from '@/services/scispaceService';
import type { ApiExtractionObservation } from '@/types/api/research-extraction-contract';

vi.mock('@/services/scispaceService', () => ({
  createMatrix: vi.fn(),
  deleteMatrix: vi.fn(),
  getExtractionTaskStatus: vi.fn(),
  getMatrix: vi.fn(),
  listMatrices: vi.fn(),
  triggerExtraction: vi.fn(),
  updateMatrix: vi.fn(),
  getCellObservations: vi.fn(),
  listFormVersions: vi.fn(),
}));

const observation = (
  id: string,
  actor: string,
  value: unknown
): ApiExtractionObservation => ({
  id,
  actor_user_id: actor,
  created_at: '2026-09-30T10:00:00Z',
  document_id: 'doc-1',
  field_id: 'field-1',
  form_version_id: 'fv-1',
  kind: 'human',
  source_hash: 'h',
  validation_state: 'valid',
  value,
});

describe('GOO-304 extraction forms UI', () => {
  it('renders missingness label and stale badge', async () => {
    vi.mocked(getMatrix).mockResolvedValue({
      id: 'm-1',
      project_id: 'p-1',
      name: 'Outcomes',
      columns: [{ name: 'Sample size' }],
      form_version: {
        id: 'fv-2',
        fields: [],
        version_no: 2,
        provenance: 'user',
        content_hash: 'abcdef0123456789',
        protocol_version_id: null,
        created_at: null,
      },
      cells: [
        {
          document_id: 'doc-1',
          column_name: 'Sample size',
          value: null,
          citation_snippet: null,
          confidence: null,
          field_id: 'field-1',
          source: 'accepted',
          missingness: 'not_reported',
          validation_state: 'valid',
          stale: true,
        },
      ],
      created_at: null,
      updated_at: null,
    });

    render(
      <ExtractionMatrix
        projectId="p-1"
        matrixId="m-1"
        documents={[{ id: 'doc-1', title: 'Paper A' }]}
      />
    );

    expect(await screen.findByText('Not reported')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Stale: the form field or source document changed')
    ).toBeInTheDocument();
    expect(screen.getByText('Form v2')).toHaveAttribute(
      'title',
      'Form hash abcdef012345…'
    );
  });

  it('hides confidence when null', async () => {
    const { user } = render(
      <CellCitation citation_snippet="n = 120" confidence={null} />
    );
    await user.click(screen.getByLabelText('View citation'));
    expect(await screen.findByText('n = 120')).toBeInTheDocument();
    expect(screen.queryByText('Confidence')).not.toBeInTheDocument();
    expect(screen.queryByText(/Low/)).not.toBeInTheDocument();
  });

  it('observations popover lists both reviewers', async () => {
    vi.mocked(listFormVersions).mockResolvedValue([]);
    vi.mocked(getCellObservations).mockResolvedValue({
      observations: [
        observation('o-1', 'aaaaaaaa-1111', 120),
        observation('o-2', 'bbbbbbbb-2222', 118),
      ],
      accepted_chain: [],
    });
    const { user } = render(
      <CellObservations
        matrixId="m-1"
        documentId="doc-1"
        fieldId="field-1"
        column="Sample size"
      />
    );

    expect(getCellObservations).not.toHaveBeenCalled();
    await user.click(screen.getByLabelText('Observations for Sample size'));

    expect(await screen.findByText(/Reviewer aaaaaaaa/)).toBeInTheDocument();
    expect(screen.getByText(/Reviewer bbbbbbbb/)).toBeInTheDocument();
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByText('118')).toBeInTheDocument();
    expect(getCellObservations).toHaveBeenCalledWith('m-1', 'doc-1', 'field-1');
  });
});
