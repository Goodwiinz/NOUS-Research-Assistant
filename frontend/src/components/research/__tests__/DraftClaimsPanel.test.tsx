vi.mock('@/hooks/useBackendCapabilities', () => ({
  useBackendCapabilities: () => ({ draftClaims: true, draftRelease: true }),
}));
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DraftClaimsPanel } from '../DraftClaimsPanel';
import { projectService } from '@/services/projectService';
import {
  listSynthesis,
  type ResearchProjectRole,
  type SynthesisList,
} from '@/services/researchEngineService';
import { APIErrorClass } from '@/types/api';
import type {
  ApiClaimLink,
  ApiClaimListResponse,
  ApiClaimSummary,
} from '@/types/api/research-claims-contract';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listClaims: vi.fn(),
    downloadClaimsExport: vi.fn(),
    createClaim: vi.fn(),
    linkClaimEvidence: vi.fn(),
    observeClaimLink: vi.fn(),
    assessClaim: vi.fn(),
  },
}));
vi.mock('@/services/researchEngineService', () => ({
  listSynthesis: vi.fn(async () => ({ results: [] })),
}));
vi.mock('@/services/scispaceService', () => ({
  listMatrices: vi.fn(async () => ({ matrices: [], total: 0 })),
  getMatrix: vi.fn(),
  getCellObservations: vi.fn(),
}));

const T = '2026-09-30T00:00:00Z';

function version(
  overrides: Partial<ApiClaimSummary['version']> = {}
): ApiClaimSummary['version'] {
  return {
    id: 'v-1',
    claim_id: 'c-1',
    version_no: 1,
    kind: 'factual',
    attributed_to_user_id: null,
    text: 'Treatment reduced mortality by 12%.',
    text_sha256: 'a'.repeat(64),
    normalized_hash: 'b'.repeat(64),
    draft_id: 'draft-1',
    draft_version: 1,
    draft_content_hash: 'c'.repeat(64),
    start_char: 7,
    end_char: 42,
    draft_review_id: null,
    created_by_id: 'user-editor',
    created_at: T,
    supersedes_claim_version_id: null,
    ...overrides,
  };
}

function link(overrides: Partial<ApiClaimLink> = {}): ApiClaimLink {
  return {
    id: 'l-1',
    claim_version_id: 'v-1',
    kind: 'source_span',
    status: 'linked',
    created_by_id: 'user-editor',
    created_at: T,
    source_changed: false,
    latest_observation: null,
    ...overrides,
  };
}

function response(items: ApiClaimSummary[]): ApiClaimListResponse {
  return {
    items,
    counts: {
      claims: items.length,
      links_by_kind: {},
      legacy_unanchored: 0,
      assessed: items.filter((i) => i.assessment).length,
      unassessed: items.filter((i) => !i.assessment).length,
    },
  };
}

function renderPanel(
  props: {
    content?: string;
    roles?: ResearchProjectRole[];
    canEdit?: boolean;
  } = {}
): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DraftClaimsPanel projectId="project-1" draftId="draft-1" {...props} />
    </QueryClientProvider>
  );
}

function linkedClaim(): ApiClaimSummary {
  return {
    claim_id: 'c-1',
    version: version(),
    is_tip: true,
    links: [link({ id: 'l-extraction', kind: 'extraction' })],
    assessment: null,
    citation_review_status: null,
  };
}

describe('DraftClaimsPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders unassessed and legacy badges', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(
      response([
        {
          claim_id: 'c-1',
          version: version({ version_no: 2 }),
          is_tip: true,
          links: [
            link({ id: 'l-legacy', kind: 'legacy_unanchored' }),
            link({ id: 'l-span', source_changed: true }),
          ],
          assessment: null,
          citation_review_status: null,
        },
        {
          claim_id: 'c-2',
          version: version({
            id: 'v-2',
            claim_id: 'c-2',
            kind: 'interpretation',
            attributed_to_user_id: 'author-12345678',
          }),
          is_tip: true,
          links: [link({ id: 'l-2', claim_version_id: 'v-2' })],
          assessment: {
            id: 'a-1',
            claim_version_id: 'v-2',
            stance: 'supporting',
            link_ids: ['l-2'],
            stance_observation_ids: [],
            rationale: 'Trial reports it.',
            assessed_by_id: 'adjudica-0000',
            actor_role: 'adjudicator',
            created_at: T,
          },
          citation_review_status: null,
        },
      ])
    );
    renderPanel();
    expect(await screen.findByLabelText('Unassessed')).toBeInTheDocument();
    expect(screen.getByLabelText('Legacy (unanchored) ×1')).toBeInTheDocument();
    expect(screen.getByLabelText('Source changed')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Accepted: supporting by adjudica')
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText('Interpretation — author-1')
    ).toBeInTheDocument();
    expect(screen.getByText('v2')).toBeInTheDocument();
    expect(projectService.listClaims).toHaveBeenCalledWith(
      'project-1',
      'draft-1'
    );
  });

  it('never renders a percentage', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(
      response([
        {
          claim_id: 'c-1',
          version: version({ text: 'Mortality fell sharply.' }),
          is_tip: true,
          links: [
            link({
              latest_observation: {
                id: 'o-1',
                link_id: 'l-1',
                stance: 'not_addressed',
                classifier_confidence: 0.87,
                classifier_version: 'cv-1',
                source_content_hash: 'd'.repeat(64),
                observed_by_id: 'user-editor',
                created_at: T,
              },
            }),
          ],
          assessment: null,
          citation_review_status: null,
        },
      ])
    );
    const { container } = renderPanel();
    expect(
      await screen.findByText('Model stance: not addressed')
    ).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/%|0\.87|87/);
  });

  it('export button calls downloadClaimsExport', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(response([]));
    vi.mocked(projectService.downloadClaimsExport).mockResolvedValue();
    renderPanel();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Export evidence' })
    );
    expect(projectService.downloadClaimsExport).toHaveBeenCalledWith(
      'project-1',
      'draft-1'
    );
  });

  it('selection offsets are code points', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(response([]));
    vi.mocked(projectService.createClaim).mockResolvedValue(
      {} as Awaited<ReturnType<typeof projectService.createClaim>>
    );
    const content = '📊 Chart\nThe trial enrolled 412 participants.';
    const passage = 'The trial enrolled 412 participants.';
    const start = content.indexOf(passage); // UTF-16: the emoji is two units
    renderPanel({ content, canEdit: true });
    const source = (await screen.findByLabelText(
      'Select a passage to claim'
    )) as HTMLTextAreaElement;
    source.setSelectionRange(start, start + passage.length);
    fireEvent.select(source);
    fireEvent.click(screen.getByRole('button', { name: 'Create claim' }));
    await waitFor(() =>
      expect(projectService.createClaim).toHaveBeenCalledWith(
        'project-1',
        expect.objectContaining({
          draft_id: 'draft-1',
          start_char: start - 1,
          end_char: start - 1 + passage.length,
          text: passage,
          kind: 'factual',
          idempotency_key: expect.any(String),
        })
      )
    );
  });

  it('assess form hidden without adjudicator role', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(
      response([linkedClaim()])
    );
    const { unmount } = renderPanel({ content: 'Body.', canEdit: true });
    expect(
      await screen.findByRole('button', { name: 'Link evidence' })
    ).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Assess' })).toBeNull();
    unmount();
    renderPanel({ content: 'Body.', canEdit: true, roles: ['adjudicator'] });
    expect(
      await screen.findByRole('group', { name: 'Assess' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: /Extraction link l-extrac/ })
    ).toBeInTheDocument();
  });

  it('links only a current computed synthesis result', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(
      response([linkedClaim()])
    );
    const base = {
      outcome_key: 'depressive_symptoms',
      timepoint: '12 weeks',
      status: 'computed',
      stale: false,
      superseded: false,
      estimate: -0.31,
    };
    vi.mocked(listSynthesis).mockResolvedValue({
      results: [
        { ...base, id: 'r-old', superseded: true },
        {
          ...base,
          id: 'r-failed',
          status: 'validation_failed',
          estimate: null,
        },
        { ...base, id: 'r-current' },
      ],
    } as unknown as SynthesisList);
    vi.mocked(projectService.linkClaimEvidence).mockResolvedValue(
      link({ kind: 'synthesis_result' })
    );
    renderPanel({ content: 'Body.', canEdit: true });
    const select = await screen.findByLabelText('Synthesis result to link');
    expect(
      [...(select as HTMLSelectElement).options].map((o) => o.value)
    ).toEqual(['', 'r-current']);
    fireEvent.change(select, { target: { value: 'r-current' } });
    fireEvent.click(screen.getByRole('button', { name: 'Link result' }));
    await waitFor(() =>
      expect(projectService.linkClaimEvidence).toHaveBeenCalledWith(
        'project-1',
        'c-1',
        expect.objectContaining({
          kind: 'synthesis_result',
          synthesis_result_id: 'r-current',
        })
      )
    );
  });

  it('stale 409 refetches claims and shows server detail', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(
      response([linkedClaim()])
    );
    vi.mocked(projectService.assessClaim).mockRejectedValue(
      new APIErrorClass({
        message: 'Assessment is stale; reload',
        status_code: 409,
        type: 'http_error',
      })
    );
    renderPanel({ content: 'Body.', canEdit: true, roles: ['adjudicator'] });
    fireEvent.change(await screen.findByLabelText('Rationale'), {
      target: { value: 'Trial reports it.' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Extraction link/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Record assessment' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Assessment is stale; reload'
    );
    expect(projectService.assessClaim).toHaveBeenCalledWith(
      'project-1',
      'c-1',
      expect.objectContaining({
        claim_version_id: 'v-1',
        stance: 'supporting',
        link_ids: ['l-extraction'],
        supersedes_assessment_id: null,
      })
    );
    await waitFor(() =>
      expect(projectService.listClaims).toHaveBeenCalledTimes(2)
    );
  });

  it('read-only without props', async () => {
    vi.mocked(projectService.listClaims).mockResolvedValue(
      response([linkedClaim()])
    );
    renderPanel();
    expect(await screen.findByLabelText('Unassessed')).toBeInTheDocument();
    expect(screen.queryByLabelText('Select a passage to claim')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Link evidence' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Create claim' })).toBeNull();
    expect(screen.queryByRole('group', { name: 'Assess' })).toBeNull();
  });
});
