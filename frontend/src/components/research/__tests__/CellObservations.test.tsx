import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@/test/test-utils';
import { CellObservations } from '../CellObservations';
import {
  acceptExtractionValue,
  createObservation,
  getCellObservations,
  listFormVersions,
} from '@/services/scispaceService';
import { listProjectRoles } from '@/services/researchEngineService';
import type {
  ApiExtractionAnchor,
  ApiExtractionObservation,
} from '@/types/api/research-extraction-contract';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn(),
}));
vi.mock('@/services/scispaceService', () => ({
  acceptExtractionValue: vi.fn(),
  createObservation: vi.fn(),
  getCellObservations: vi.fn(),
  listFormVersions: vi.fn(),
}));

const anchor = (
  over: Partial<ApiExtractionAnchor> = {}
): ApiExtractionAnchor => ({
  status: 'verified',
  start_char: 10,
  end_char: 17,
  page: 3,
  occurrences: [10],
  occurrences_in_text: 1,
  occurrence_contexts: [],
  ...over,
});

const obs = (
  id: string,
  over: Partial<ApiExtractionObservation> = {}
): ApiExtractionObservation => ({
  id,
  actor_user_id: 'aaaaaaaa-1111',
  created_at: '2026-09-30T10:00:00Z',
  document_id: 'doc-1',
  field_id: 'field-1',
  form_version_id: 'fv-1',
  kind: 'machine',
  extractor_model: 'stub',
  source_hash: 'h',
  validation_state: 'valid',
  value: '120',
  citation: 'n = 120',
  anchor: anchor(),
  context_before: 'We enrolled ',
  context_after: ' adults.',
  text_length: 1000,
  inspected_coverage: [[0, 1000]],
  coverage_complete: true,
  source_changed: false,
  ...over,
});

async function openDrawer(
  observations: ApiExtractionObservation[],
  role: 'adjudicator' | 'reviewer' | null = 'adjudicator'
): Promise<ReturnType<typeof render>> {
  vi.mocked(getCellObservations).mockResolvedValue({
    observations,
    accepted_chain: [],
  });
  vi.mocked(listProjectRoles).mockResolvedValue(
    role
      ? [
          {
            id: 'r',
            project_id: 'p-1',
            user_id: 'me',
            role,
            assigned_by_id: 'o',
            created_at: '2026-09-30T10:00:00Z',
          },
        ]
      : []
  );
  const utils = render(
    <CellObservations
      projectId="p-1"
      matrixId="m-1"
      documentId="doc-1"
      fieldId="field-1"
      column="Sample size"
      anchorStatus={observations[0]?.anchor?.status}
    />
  );
  await utils.user.click(screen.getByLabelText(/^Evidence: /));
  await screen.findByRole('heading', { name: 'Evidence' });
  return utils;
}

const acceptButton = (): HTMLElement =>
  screen.getByRole('button', { name: 'Accept' });

async function decide(
  user: Awaited<ReturnType<typeof openDrawer>>['user'],
  label: string,
  rationale = 'matches methods'
): Promise<void> {
  await waitFor(() =>
    expect(screen.getByRole('heading', { name: 'Decide' })).toBeInTheDocument()
  );
  await user.selectOptions(screen.getByLabelText('Accepted value'), label);
  if (rationale) await user.type(screen.getByLabelText('Rationale'), rationale);
}

describe('GOO-305 evidence drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listFormVersions).mockResolvedValue([
      {
        id: 'fv-1',
        matrix_id: 'm-1',
        version_no: 1,
        provenance: 'authored',
        fields: [],
        content_hash: 'c',
        created_at: '2026-09-30T10:00:00Z',
      },
    ]);
    vi.mocked(acceptExtractionValue).mockResolvedValue(
      {} as Awaited<ReturnType<typeof acceptExtractionValue>>
    );
  });

  it('renders verified evidence with citation, page and offsets', async () => {
    await openDrawer([obs('o-1')]);
    expect(
      screen.getByText('n = 120', { selector: 'mark' })
    ).toBeInTheDocument();
    expect(screen.getByText(/p\. 3 · chars 10–17 of 1000/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Confidence|\d%\)/);
  });

  it('blocks Accept on an ambiguous anchor until an occurrence is chosen', async () => {
    const ambiguous = obs('o-1', {
      anchor: anchor({
        status: 'ambiguous',
        start_char: null,
        end_char: null,
        page: null,
        occurrences: [4, 40],
        occurrences_in_text: 2,
        occurrence_contexts: [
          {
            start_char: 4,
            end_char: 11,
            page: 1,
            context_before: 'a ',
            context_after: ' b',
          },
          {
            start_char: 40,
            end_char: 47,
            page: 2,
            context_before: 'c ',
            context_after: ' d',
          },
        ],
      }),
      context_before: null,
      context_after: null,
    });
    const { user } = await openDrawer([ambiguous]);
    expect(screen.getByText(/Appears 2× in document/)).toBeInTheDocument();
    await decide(user, '120');
    expect(acceptButton()).toBeDisabled();
    await user.click(screen.getAllByRole('radio')[1]);
    expect(acceptButton()).toBeEnabled();
    await user.click(acceptButton());
    await waitFor(() => expect(acceptExtractionValue).toHaveBeenCalledOnce());
    const [, body] = vi.mocked(acceptExtractionValue).mock.calls[0];
    expect(body).toMatchObject({
      anchor_start: 40,
      accept_unverified: false,
      observation_ids: ['o-1'],
      value: '120',
      rationale: 'matches methods',
      supersedes_accepted_value_id: null,
    });
    expect(body.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('requires the checkbox to accept an unverified anchor', async () => {
    const unverified = obs('o-1', {
      anchor: anchor({
        status: 'unverified',
        start_char: null,
        end_char: null,
        page: null,
      }),
      context_before: null,
      context_after: null,
    });
    const { user } = await openDrawer([unverified]);
    await decide(user, '120');
    expect(acceptButton()).toBeDisabled();
    await user.click(
      screen.getByRole('checkbox', {
        name: 'Accept without a verified source location',
      })
    );
    expect(acceptButton()).toBeEnabled();
    await user.click(acceptButton());
    await waitFor(() => expect(acceptExtractionValue).toHaveBeenCalledOnce());
    expect(
      vi.mocked(acceptExtractionValue).mock.calls[0][1].accept_unverified
    ).toBe(true);
  });

  it('warns on partial coverage', async () => {
    await openDrawer([
      obs('o-1', {
        value: null,
        missingness: 'unavailable_text',
        anchor: null,
        citation: null,
        inspected_coverage: [[0, 250]],
        coverage_complete: false,
      }),
    ]);
    expect(screen.getByText(/Inspected 25% of text/)).toBeInTheDocument();
    expect(screen.getByText(/Partial coverage/)).toBeInTheDocument();
  });

  it('disables Decide when the source changed', async () => {
    const { user } = await openDrawer([
      obs('o-1', {
        source_changed: true,
        context_before: null,
        context_after: null,
      }),
    ]);
    await decide(user, '120');
    expect(
      screen.getAllByText('Source changed since extraction; re-run extraction')
        .length
    ).toBeGreaterThan(0);
    expect(acceptButton()).toBeDisabled();
  });

  it('renders a disagreement side by side in two columns', async () => {
    await openDrawer([
      obs('o-1'),
      obs('o-2', {
        value: '118',
        kind: 'human',
        actor_user_id: 'bbbbbbbb-2222',
      }),
    ]);
    const columns = screen.getByTestId('observation-columns');
    expect(columns).toHaveClass('grid-cols-2');
    expect(columns.querySelectorAll(':scope > li')).toHaveLength(2);
    expect(
      screen.getByRole('heading', { name: 'Disagreement' })
    ).toBeInTheDocument();
  });

  it('requires a rationale', async () => {
    const { user } = await openDrawer([obs('o-1')]);
    await decide(user, '120', '');
    expect(acceptButton()).toBeDisabled();
    await user.type(screen.getByLabelText('Rationale'), 'methods');
    expect(acceptButton()).toBeEnabled();
  });

  it('shows Add evidence but not Decide to a reviewer', async () => {
    vi.mocked(createObservation).mockResolvedValue(
      {} as Awaited<ReturnType<typeof createObservation>>
    );
    const { user } = await openDrawer([obs('o-1')], 'reviewer');
    await screen.findByRole('heading', { name: 'Add evidence' });
    expect(
      screen.queryByRole('heading', { name: 'Decide' })
    ).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('Value'), '121');
    await user.type(
      screen.getByLabelText('Verbatim citation from the document'),
      'n = 121'
    );
    await user.click(screen.getByRole('button', { name: 'Add evidence' }));
    await waitFor(() => expect(createObservation).toHaveBeenCalledOnce());
    expect(vi.mocked(createObservation).mock.calls[0][1]).toMatchObject({
      form_version_id: 'fv-1',
      value: '121',
      citation: 'n = 121',
      anchor_start: null,
    });
  });
});
