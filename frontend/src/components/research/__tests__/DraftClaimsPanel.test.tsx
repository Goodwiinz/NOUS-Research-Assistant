vi.mock('@/hooks/useBackendCapabilities', () => ({
  useBackendCapabilities: () => ({ draftClaims: true, draftRelease: true }),
}));
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DraftClaimsPanel } from '../DraftClaimsPanel';
import { projectService } from '@/services/projectService';
import type {
  ApiClaimLink,
  ApiClaimListResponse,
  ApiClaimSummary,
} from '@/types/api/research-claims-contract';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listClaims: vi.fn(),
    downloadClaimsExport: vi.fn(),
  },
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

function renderPanel(): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DraftClaimsPanel projectId="project-1" draftId="draft-1" />
    </QueryClientProvider>
  );
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
});
