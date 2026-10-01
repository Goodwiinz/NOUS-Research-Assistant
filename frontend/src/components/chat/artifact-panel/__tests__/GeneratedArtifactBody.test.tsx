import { act, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/artifactService', () => ({
  artifactService: {
    listVersions: vi.fn(),
    fetchVersionBlob: vi.fn(),
    downloadVersion: vi.fn(),
  },
}));

import {
  artifactService,
  type ArtifactVersion,
} from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { render } from '@/test/test-utils';
import { GeneratedArtifactBody } from '../GeneratedArtifactBody';

const v = (
  id: string,
  createdAt: string,
  title = 'report.md'
): ArtifactVersion => ({
  artifactId: 'a1',
  versionId: id,
  parentVersionId: null,
  title,
  mimeType: 'text/plain',
  byteSize: 7,
  sha256: 'x',
  createdAt,
  producer: 'harness',
  sourceIds: [],
});

describe('GeneratedArtifactBody', () => {
  it('lists versions newest first, shows provenance, and re-targets the panel on switch', async () => {
    act(() =>
      useArtifactPanelStore.setState({
        artifact: null,
        isOpen: false,
        pinned: false,
      })
    );
    vi.mocked(artifactService.listVersions).mockResolvedValue([
      v('v1', '2026-09-30T00:00:00Z', 'draft.md'),
      v('v2', '2026-09-30T01:00:00Z'),
    ]);
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
      new Blob(['body'], { type: 'text/plain' })
    );
    const { user } = render(
      <GeneratedArtifactBody
        artifact={{
          kind: 'generated',
          artifactId: 'a1',
          versionId: 'v2',
          title: 'report.md',
        }}
      />
    );
    const select = (await screen.findByLabelText(
      'Artifact version'
    )) as HTMLSelectElement;
    expect(select.value).toBe('v2');
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      'v2',
      'v1',
    ]);
    expect(screen.getByLabelText('Provenance')).toHaveTextContent(
      'v2 · Published by Codex · text/plain · 7 B'
    );
    expect(await screen.findByText('body')).toBeInTheDocument();
    await user.selectOptions(select, 'v1');
    expect(useArtifactPanelStore.getState().artifact).toEqual({
      kind: 'generated',
      artifactId: 'a1',
      versionId: 'v1',
      title: 'draft.md',
    });
  });

  it('labels member-published versions neutrally', async () => {
    vi.mocked(artifactService.listVersions).mockResolvedValue([
      { ...v('v1', '2026-09-30T00:00:00Z'), producer: 'user' },
    ]);
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
      new Blob(['body'], { type: 'text/plain' })
    );
    render(
      <GeneratedArtifactBody
        artifact={{
          kind: 'generated',
          artifactId: 'a1',
          versionId: 'v1',
          title: 'report.md',
        }}
      />
    );
    expect(await screen.findByLabelText('Provenance')).toHaveTextContent(
      'Published by a project member'
    );
    expect(screen.getByLabelText('Provenance')).not.toHaveTextContent('by you');
  });

  it('drops a download failure when another version is selected', async () => {
    vi.mocked(artifactService.listVersions).mockResolvedValue([
      v('v1', '2026-09-30T00:00:00Z'),
      v('v2', '2026-09-30T01:00:00Z'),
    ]);
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
      new Blob(['body'], { type: 'text/plain' })
    );
    vi.mocked(artifactService.downloadVersion).mockRejectedValue(
      new Error('nope')
    );
    const base = {
      kind: 'generated' as const,
      artifactId: 'a1',
      title: 'report.md',
    };
    const { user, rerender } = render(
      <GeneratedArtifactBody artifact={{ ...base, versionId: 'v2' }} />
    );
    await screen.findByLabelText('Artifact version');
    await user.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Download failed.'
    );
    rerender(<GeneratedArtifactBody artifact={{ ...base, versionId: 'v1' }} />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('refetches instead of showing an older version when the requested one is not cached', async () => {
    vi.mocked(artifactService.listVersions)
      .mockResolvedValueOnce([v('v1', '2026-09-30T00:00:00Z')])
      .mockResolvedValueOnce([
        v('v1', '2026-09-30T00:00:00Z'),
        v('v9', '2026-09-30T02:00:00Z'),
      ]);
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
      new Blob(['new'], { type: 'text/plain' })
    );
    render(
      <GeneratedArtifactBody
        artifact={{
          kind: 'generated',
          artifactId: 'a1',
          versionId: 'v9',
          title: 'report.md',
        }}
      />
    );
    expect(await screen.findByText('new')).toBeInTheDocument();
    expect(artifactService.listVersions).toHaveBeenCalledTimes(2);
  });
});
