import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProjectArtifactsTab } from '@/components/research/ProjectArtifactsTab';
import { artifactService, toProjectArtifact } from '@/services/artifactService';
import type { ApiProjectArtifact } from '@/types/api/artifact-contract';
import { render, screen, waitFor } from '@/test/test-utils';

vi.mock('@/services/artifactService', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/services/artifactService')>();
  return {
    ...actual,
    artifactService: {
      ...actual.artifactService,
      listProjectArtifacts: vi.fn(),
      fetchVersionBlob: vi.fn(),
    },
  };
});

const SHA = 'abcdef0123456789'.repeat(4);

function row(over: Partial<ApiProjectArtifact> = {}): ApiProjectArtifact {
  return {
    artifact_id: 'art-1',
    kind: 'report',
    title: 'Findings report',
    thread_id: 'thread-9',
    updated_at: '2026-10-05T10:00:00Z',
    current_version: {
      artifact_id: 'art-1',
      version_id: 'ver-1',
      parent_version_id: null,
      title: 'Findings report',
      mime_type: 'text/markdown',
      byte_size: 12,
      sha256: SHA,
      created_at: '2026-10-05T10:00:00Z',
      provenance: { producer: 'harness', source_ids: [] },
    },
    ...over,
  };
}

describe('ProjectArtifactsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a row per artifact with title, mime, sha prefix and chat link', async () => {
    vi.mocked(artifactService.listProjectArtifacts).mockResolvedValue([
      toProjectArtifact(row()),
    ]);
    render(<ProjectArtifactsTab projectId="p1" />);
    expect(await screen.findByText('Findings report')).toBeInTheDocument();
    expect(screen.getByText(/text\/markdown/)).toBeInTheDocument();
    expect(screen.getByText(SHA.slice(0, 12))).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /open in chat/i })).toHaveAttribute(
      'href',
      '/chat?thread=thread-9'
    );
  });

  it('omits the chat link when the artifact has no thread', async () => {
    vi.mocked(artifactService.listProjectArtifacts).mockResolvedValue([
      toProjectArtifact(row({ thread_id: null })),
    ]);
    render(<ProjectArtifactsTab projectId="p1" />);
    await screen.findByText('Findings report');
    expect(screen.queryByRole('link', { name: /open in chat/i })).toBeNull();
  });

  it('downloads the current version by id', async () => {
    vi.mocked(artifactService.listProjectArtifacts).mockResolvedValue([
      toProjectArtifact(row()),
    ]);
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
      new Blob(['x'])
    );
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const { user } = render(<ProjectArtifactsTab projectId="p1" />);
    await user.click(await screen.findByRole('button', { name: /download/i }));
    await waitFor(() =>
      expect(artifactService.fetchVersionBlob).toHaveBeenCalledWith('ver-1')
    );
    await waitFor(() =>
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x')
    );
  });

  it('shows an empty state', async () => {
    vi.mocked(artifactService.listProjectArtifacts).mockResolvedValue([]);
    render(<ProjectArtifactsTab projectId="p1" />);
    expect(await screen.findByText(/no files yet/i)).toBeInTheDocument();
  });
});

beforeEach(() => {
  useAuthStore.setState({
    user: { id: 'actor', organization_id: 'org' } as User,
    isAuthenticated: true,
  });
});
