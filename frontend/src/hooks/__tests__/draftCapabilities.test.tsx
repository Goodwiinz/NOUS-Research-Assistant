import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@/test/test-utils';
import { DraftViewer } from '@/components/research/DraftViewer';
import { DraftClaimsPanel } from '@/components/research/DraftClaimsPanel';
import { projectService, type Draft } from '@/services/projectService';
import { listProjectRoles } from '@/services/researchEngineService';
import { fetchBackendCapabilities } from '@/services/backendCapabilities';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/projectService', () => ({
  projectService: { getDraftRelease: vi.fn(), listClaims: vi.fn() },
}));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn(),
}));
// Mock the capability transport, retaining the shared Query owner and gating.
// The transport lives in a separate service so module-local calls can be mocked.
vi.mock('@/services/backendCapabilities', () => ({
  fetchBackendCapabilities: vi.fn(),
}));

const draft: Draft = {
  id: 'd1',
  project_id: 'p1',
  version: 1,
  title: 'Draft',
  content: 'Existing content',
  themes: [],
  word_count: 2,
  citation_count: 0,
  is_current: true,
  created_at: '2026-10-01T00:00:00Z',
};

function show(client?: QueryClient): ReturnType<typeof render> {
  const wrapper = client
    ? ({ children }: { children: React.ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      )
    : undefined;
  return render(
    <>
      <DraftViewer draft={draft} projectId="p1" />
      <DraftClaimsPanel projectId="p1" draftId="d1" />
    </>,
    client ? { wrapper } : {}
  );
}

describe('draft controls across backend releases', () => {
  beforeEach(() => vi.clearAllMocks());
  it('keeps content visible without calling unsupported APIs', async () => {
    vi.mocked(fetchBackendCapabilities).mockResolvedValue({
      draftClaims: false,
      draftRelease: false,
    });
    show();
    await waitFor(() => expect(fetchBackendCapabilities).toHaveBeenCalled());
    expect(screen.getByText('Existing content')).toBeInTheDocument();
    expect(projectService.getDraftRelease).not.toHaveBeenCalled();
    expect(projectService.listClaims).not.toHaveBeenCalled();
    expect(listProjectRoles).not.toHaveBeenCalled();
    expect(
      screen.queryByRole('button', { name: 'Promote to verified' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText('Release status: Candidate')
    ).not.toBeInTheDocument();
  });
  it('fails closed when capability discovery is unavailable', async () => {
    vi.mocked(fetchBackendCapabilities).mockRejectedValue(
      new Error('Unavailable')
    );
    show();
    await waitFor(() => expect(fetchBackendCapabilities).toHaveBeenCalled());
    expect(projectService.getDraftRelease).not.toHaveBeenCalled();
    expect(projectService.listClaims).not.toHaveBeenCalled();
    expect(listProjectRoles).not.toHaveBeenCalled();
  });
  it('hides cached controls after backend rollback', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.mocked(fetchBackendCapabilities).mockResolvedValue({
      draftClaims: true,
      draftRelease: true,
    });
    vi.mocked(projectService.getDraftRelease).mockResolvedValue({
      draft_id: 'd1',
      draft_version: 1,
      release_status: 'candidate',
      content_hash: '',
      blockers: [],
      dimensions: {},
      release: null,
      invalidation: null,
    });
    vi.mocked(projectService.listClaims).mockResolvedValue({
      items: [],
      counts: {
        claims: 0,
        assessed: 0,
        unassessed: 0,
        legacy_unanchored: 0,
        links_by_kind: {},
      },
    });
    vi.mocked(listProjectRoles).mockResolvedValue([]);
    const view = show(client);
    await screen.findByRole('region', { name: 'Claims' });
    await screen.findByLabelText('Release status: Candidate');
    expect(fetchBackendCapabilities).toHaveBeenCalledTimes(1);
    const releaseCalls = vi.mocked(projectService.getDraftRelease).mock.calls
      .length;
    const claimsCalls = vi.mocked(projectService.listClaims).mock.calls.length;
    vi.mocked(fetchBackendCapabilities).mockResolvedValue({
      draftClaims: false,
      draftRelease: false,
    });
    await act(async () => {
      await client.invalidateQueries({ queryKey: ['backend-capabilities'] });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole('region', { name: 'Claims' })
      ).not.toBeInTheDocument()
    );
    expect(
      screen.queryByLabelText('Release status: Candidate')
    ).not.toBeInTheDocument();
    expect(projectService.getDraftRelease).toHaveBeenCalledTimes(releaseCalls);
    expect(projectService.listClaims).toHaveBeenCalledTimes(claimsCalls);
    view.unmount();
    client.clear();
  });
  it('waits for capability discovery before calling APIs', async () => {
    let resolve!: (value: {
      draftClaims: boolean;
      draftRelease: boolean;
    }) => void;
    vi.mocked(fetchBackendCapabilities).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      })
    );
    vi.mocked(projectService.getDraftRelease).mockResolvedValue({
      draft_id: 'd1',
      draft_version: 1,
      release_status: 'candidate',
      content_hash: '',
      blockers: [],
      dimensions: {},
      release: null,
      invalidation: null,
    });
    vi.mocked(projectService.listClaims).mockResolvedValue({
      items: [],
      counts: {
        claims: 0,
        assessed: 0,
        unassessed: 0,
        legacy_unanchored: 0,
        links_by_kind: {},
      },
    });
    vi.mocked(listProjectRoles).mockResolvedValue([]);
    show();
    await waitFor(() => expect(fetchBackendCapabilities).toHaveBeenCalled());
    expect(projectService.getDraftRelease).not.toHaveBeenCalled();
    expect(projectService.listClaims).not.toHaveBeenCalled();
    resolve({ draftClaims: true, draftRelease: true });
    await waitFor(() =>
      expect(projectService.getDraftRelease).toHaveBeenCalledWith('p1', 'd1', 1)
    );
    await waitFor(() =>
      expect(projectService.listClaims).toHaveBeenCalledWith('p1', 'd1')
    );
  });
});
