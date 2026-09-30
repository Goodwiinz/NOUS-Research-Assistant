import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/artifactService', () => ({
  artifactService: { fetchVersionBlob: vi.fn(), downloadVersion: vi.fn() },
}));

import { artifactService, type ArtifactVersion } from '@/services/artifactService';
import { render } from '@/test/test-utils';
import {
  ArtifactPreview,
  MAX_TEXT_PREVIEW_BYTES,
  previewKindFor,
} from '../ArtifactPreview';

const base: ArtifactVersion = {
  artifactId: 'a1',
  versionId: 'v1',
  parentVersionId: null,
  title: 'report.md',
  mimeType: 'text/markdown',
  byteSize: 7,
  sha256: 'x',
  createdAt: '2026-09-30T00:00:00Z',
  producer: 'harness',
  sourceIds: [],
};

describe('ArtifactPreview', () => {
  it('chooses markdown, text, image or download by type and size', () => {
    expect(previewKindFor(base)).toBe('markdown');
    expect(previewKindFor({ ...base, mimeType: 'text/plain' })).toBe('text');
    expect(previewKindFor({ ...base, mimeType: 'image/png' })).toBe('image');
    expect(previewKindFor({ ...base, mimeType: 'text/html' })).toBe('download');
    expect(previewKindFor({ ...base, mimeType: 'application/pdf' })).toBe('download');
    expect(previewKindFor({ ...base, byteSize: MAX_TEXT_PREVIEW_BYTES + 1 })).toBe('download');
  });

  it('renders markdown without executing embedded html', async () => {
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
      new Blob(['# Title\n\n<img src=x onerror="window.__pwned=1">'], { type: 'text/markdown' })
    );
    render(<ArtifactPreview version={base} />);
    expect(await screen.findByRole('heading', { name: 'Title' })).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('shows plain text escaped', async () => {
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
      new Blob(['<b>not bold</b>'], { type: 'text/plain' })
    );
    render(<ArtifactPreview version={{ ...base, mimeType: 'text/plain' }} />);
    expect(await screen.findByText('<b>not bold</b>')).toBeInTheDocument();
    expect(document.querySelector('b')).toBeNull();
  });

  it('offers download for unsupported types without fetching', async () => {
    const { user } = render(<ArtifactPreview version={{ ...base, mimeType: 'text/html', title: 'tool.html' }} />);
    expect(artifactService.fetchVersionBlob).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /Download tool.html/ }));
    expect(artifactService.downloadVersion).toHaveBeenCalled();
  });

  it('surfaces a stable error with retry', async () => {
    vi.mocked(artifactService.fetchVersionBlob)
      .mockRejectedValueOnce(new Error('403 secret detail'))
      .mockResolvedValueOnce(new Blob(['ok'], { type: 'text/plain' }));
    const { user } = render(<ArtifactPreview version={{ ...base, mimeType: 'text/plain' }} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load this file.');
    expect(alert).not.toHaveTextContent('secret');
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('ok')).toBeInTheDocument());
  });
});
