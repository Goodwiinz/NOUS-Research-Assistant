vi.mock('@/hooks/useBackendCapabilities', () => ({
  useBackendCapabilities: () => ({ draftClaims: true, draftRelease: true }),
}));
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@/test/test-utils';
import { DraftViewer } from '../DraftViewer';
import { projectService, type Draft } from '@/services/projectService';
import { listProjectRoles } from '@/services/researchEngineService';
import type { ApiReleaseCheck } from '@/types/api/research-release-contract';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn(),
}));
vi.mock('@/services/projectService', () => ({
  projectService: { getDraftRelease: vi.fn(), promoteDraft: vi.fn() },
}));

const draft: Draft = {
  id: 'draft-1',
  project_id: 'project-1',
  version: 2,
  title: 'Review',
  content: 'Mortality fell by 40 percent [Doc 1].',
  themes: [],
  word_count: 7,
  citation_count: 1,
  is_current: true,
  created_at: '2026-09-30T00:00:00Z',
  content_hash: 'a'.repeat(64),
};

function check(overrides: Partial<ApiReleaseCheck> = {}): ApiReleaseCheck {
  return {
    draft_id: 'draft-1',
    draft_version: 2,
    release_status: 'candidate',
    content_hash: 'a'.repeat(64),
    blockers: [],
    dimensions: {},
    release: null,
    invalidation: null,
    ...overrides,
  };
}

function withRole(role: 'adjudicator' | 'reviewer' | null): void {
  vi.mocked(listProjectRoles).mockResolvedValue(
    role
      ? [
          {
            id: 'r',
            project_id: 'project-1',
            user_id: 'me',
            role,
            assigned_by_id: 'o',
            created_at: '2026-09-30T00:00:00Z',
          },
        ]
      : []
  );
}

describe('DraftViewer release status (GOO-307)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders candidate badge by default', async () => {
    withRole(null);
    vi.mocked(projectService.getDraftRelease).mockResolvedValue(check());
    render(<DraftViewer draft={draft} projectId="project-1" />);
    expect(
      await screen.findByLabelText('Release status: Candidate')
    ).toBeInTheDocument();
    expect(projectService.getDraftRelease).toHaveBeenCalledWith(
      'project-1',
      'draft-1',
      2
    );
  });

  it('lists blockers and disables promote', async () => {
    withRole('adjudicator');
    vi.mocked(projectService.getDraftRelease).mockResolvedValue(
      check({
        blockers: [
          {
            code: 'model_only',
            claim_version_id: 'cv-4',
            start: 0,
            end: 37,
            text: 'Mortality fell by 40 percent [Doc 1].',
            detail: 'Only a model stance; no adjudicator assessment',
          },
        ],
      })
    );
    render(<DraftViewer draft={draft} projectId="project-1" />);
    const list = await screen.findByRole('list', { name: 'Release blockers' });
    expect(list).toHaveTextContent('Model stance only:');
    expect(list).toHaveTextContent('Mortality fell by 40 percent [Doc 1].');
    expect(
      screen.getByRole('button', { name: 'Promote to verified' })
    ).toBeDisabled();
  });

  it('hides promote without role', async () => {
    withRole('reviewer');
    vi.mocked(projectService.getDraftRelease).mockResolvedValue(check());
    render(<DraftViewer draft={draft} projectId="project-1" />);
    await screen.findByLabelText('Release status: Candidate');
    await waitFor(() => expect(listProjectRoles).toHaveBeenCalled());
    expect(
      screen.queryByRole('button', { name: 'Promote to verified' })
    ).not.toBeInTheDocument();
  });

  it('shows stale cause', async () => {
    withRole(null);
    vi.mocked(projectService.getDraftRelease).mockResolvedValue(
      check({
        release_status: 'stale',
        invalidation: {
          stale_at: '2026-09-30T01:00:00Z',
          cause: {
            family: 'research_extraction',
            event_id: 'e-1',
            kind: 'source_changed',
          },
          changed_nodes: ['accepted:a-1'],
          assessment_ids: [],
        },
      })
    );
    render(<DraftViewer draft={draft} projectId="project-1" />);
    expect(
      await screen.findByLabelText('Release status: Stale')
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Stale — invalidated by research_extraction (source_changed)'
    );
  });
});
