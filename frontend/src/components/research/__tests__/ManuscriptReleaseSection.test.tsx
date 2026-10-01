import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/test-utils';
import { ManuscriptReleaseSection } from '../ManuscriptReleaseSection';
import { projectService, type Draft } from '@/services/projectService';
import type {
  ApiCheckResult,
  ApiManuscriptRelease,
} from '@/types/api/manuscript-release-contract';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listManuscriptReleases: vi.fn(),
    createCandidateRelease: vi.fn(),
    promoteManuscriptRelease: vi.fn(),
    downloadManuscriptPackage: vi.fn(),
    verifyManuscriptRelease: vi.fn(),
  },
}));

const draft: Draft = {
  id: 'draft-1',
  project_id: 'project-1',
  version: 1,
  title: 'Results',
  content: 'Alpha holds.',
  themes: [],
  word_count: 2,
  citation_count: 0,
  is_current: true,
  created_at: '2026-10-01T00:00:00Z',
  content_hash: 'a'.repeat(64),
};

const pass: ApiCheckResult = { state: 'pass', items: [] };

function release(
  overrides: Partial<ApiManuscriptRelease> = {}
): ApiManuscriptRelease {
  return {
    id: 'release-candidate-1',
    collection_id: 'project-1',
    draft_id: 'draft-1',
    draft_version: 1,
    content_hash: 'a'.repeat(64),
    stage: 'candidate',
    status: 'candidate',
    stale_cause: null,
    candidate_release_id: null,
    draft_release_id: null,
    snapshot_hash: 'b'.repeat(64),
    checks: {
      claim_support: {
        state: 'fail',
        items: [{ code: 'unassessed', detail: 'No adjudicator assessment' }],
      },
      method_adherence: pass,
      synthesis_appraisal: { state: 'not_applicable', items: [] },
      peer_review: {
        state: 'fail',
        items: [{ code: 'open_comment', detail: 'open; anchor exact' }],
      },
      reporting_completeness: { state: 'not_applicable', items: [] },
      experiment_reproducibility: { state: 'unknown', items: [] },
    },
    checks_hash: 'c'.repeat(64),
    failing_obligations: ['claim_support', 'peer_review'],
    package_files: [],
    package_sha256: 'd'.repeat(64),
    created_by_id: 'me',
    actor_role: 'editor',
    created_at: '2026-10-01T00:00:00Z',
    external_submission: 'not_authorized',
    ...overrides,
  };
}

function withReleases(...releases: ApiManuscriptRelease[]): void {
  vi.mocked(projectService.listManuscriptReleases).mockResolvedValue({
    releases,
  });
}

describe('ManuscriptReleaseSection (GOO-315)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('checks rendered separately', async () => {
    withReleases(release());
    render(
      <ManuscriptReleaseSection
        projectId="project-1"
        draft={draft}
        canPromote
      />
    );
    const checks = await screen.findByLabelText('Release checks');
    for (const label of [
      'Claim support: Fail',
      'Method adherence: Pass',
      'Synthesis and appraisal: Not applicable',
      'Peer review: Fail',
      'Reporting completeness: Not applicable',
      'Experiment reproducibility: Unknown',
    ]) {
      expect(within(checks).getByLabelText(label)).toBeInTheDocument();
    }
    expect(
      within(checks).getByText(/No adjudicator assessment/)
    ).toBeInTheDocument();
  });

  it('promote disabled with failing obligations', async () => {
    withReleases(release());
    render(
      <ManuscriptReleaseSection
        projectId="project-1"
        draft={draft}
        canPromote
      />
    );
    const promote = await screen.findByRole('button', {
      name: /Promote to verified release/,
    });
    expect(promote).toBeDisabled();
    expect(
      screen.getByText('Failing obligations: Claim support, Peer review')
    ).toBeInTheDocument();
  });

  it('hides promote without an adjudicator or supervisor role', async () => {
    withReleases(release({ failing_obligations: [] }));
    render(
      <ManuscriptReleaseSection
        projectId="project-1"
        draft={draft}
        canPromote={false}
      />
    );
    await screen.findByLabelText('Release checks');
    expect(
      screen.queryByRole('button', { name: /Promote to verified release/ })
    ).not.toBeInTheDocument();
  });

  it('verified stale shown with cause', async () => {
    withReleases(
      release({
        id: 'release-verified-1',
        stage: 'verified',
        status: 'stale',
        stale_cause: 'draft_release_invalidated',
        candidate_release_id: 'release-candidate-1',
        draft_release_id: 'dr-1',
        failing_obligations: [],
        actor_role: 'adjudicator',
      })
    );
    render(
      <ManuscriptReleaseSection
        projectId="project-1"
        draft={draft}
        canPromote
      />
    );
    expect(
      await screen.findByLabelText('Manuscript release status: stale')
    ).toBeInTheDocument();
    expect(
      screen.getByText(/its verified draft release was invalidated/)
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Promote to verified release/ })
    ).not.toBeInTheDocument();
  });

  it('submission disclaimer visible', async () => {
    withReleases();
    render(
      <ManuscriptReleaseSection
        projectId="project-1"
        draft={draft}
        canPromote={false}
      />
    );
    expect(
      screen.getByText('Not authorized for external submission.')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Build candidate' })
    ).toBeEnabled();
  });
});
