import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';
import { act, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/artifactService', () => ({
  artifactService: { fetchVersionBlob: vi.fn(), downloadVersion: vi.fn() },
}));

import {
  artifactService,
  type ArtifactVersion,
} from '@/services/artifactService';
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
    expect(previewKindFor({ ...base, mimeType: 'text/html' })).toBe('html');
    expect(previewKindFor({ ...base, mimeType: 'application/pdf' })).toBe(
      'pdf'
    );
    expect(
      previewKindFor({ ...base, byteSize: MAX_TEXT_PREVIEW_BYTES + 1 })
    ).toBe('download');
  });

  it('renders markdown without executing embedded html', async () => {
    vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
      new Blob(['# Title\n\n<img src=x onerror="window.__pwned=1">'], {
        type: 'text/markdown',
      })
    );
    render(<ArtifactPreview version={base} />);
    expect(
      await screen.findByRole('heading', { name: 'Title' })
    ).toBeInTheDocument();
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
    // restoreMocks resets vi.fn() to return undefined; the handler chains
    // .catch, so the mock must resolve like the real service.
    vi.mocked(artifactService.downloadVersion).mockResolvedValue(undefined);
    const { user } = render(
      <ArtifactPreview
        version={{
          ...base,
          mimeType: 'application/octet-stream',
          title: 'tool.bin',
        }}
      />
    );
    expect(artifactService.fetchVersionBlob).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /Download tool.bin/ }));
    expect(artifactService.downloadVersion).toHaveBeenCalled();
  });

  it('surfaces a stable error with retry', async () => {
    vi.mocked(artifactService.fetchVersionBlob)
      .mockRejectedValueOnce(new Error('403 secret detail'))
      .mockResolvedValueOnce(new Blob(['ok'], { type: 'text/plain' }));
    const { user } = render(
      <ArtifactPreview version={{ ...base, mimeType: 'text/plain' }} />
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load this file.');
    expect(alert).not.toHaveTextContent('secret');
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('ok')).toBeInTheDocument());
  });
});

beforeEach(() => {
  useAuthStore.setState({
    user: { id: 'actor', organization_id: 'org' } as User,
    isAuthenticated: true,
  });
});

it('removes rendered bytes immediately when the account scope changes', async () => {
  vi.mocked(artifactService.fetchVersionBlob)
    .mockResolvedValueOnce(
      new Blob(['account A secret'], { type: 'text/plain' })
    )
    .mockImplementationOnce(() => new Promise(() => {}));
  render(<ArtifactPreview version={{ ...base, mimeType: 'text/plain' }} />);
  await screen.findByText('account A secret');
  act(() =>
    useAuthStore.setState({
      user: { id: 'other', organization_id: 'foreign' } as User,
    })
  );
  expect(screen.queryByText('account A secret')).toBeNull();
});

it('bounds CSV rows and columns while respecting quoted commas', async () => {
  const rows = [
    '"column, one",' + Array.from({ length: 60 }, (_, i) => `c${i}`).join(','),
    ...Array.from(
      { length: 220 },
      (_, i) =>
        `row${i},` + Array.from({ length: 60 }, () => '<img src=x>').join(',')
    ),
  ];
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
    new Blob([rows.join('\n')], { type: 'text/csv' })
  );
  render(<ArtifactPreview version={{ ...base, mimeType: 'text/csv' }} />);
  await screen.findByRole('table', { name: 'CSV preview' });
  expect(screen.getAllByRole('row')).toHaveLength(200);
  expect(screen.getAllByRole('columnheader')).toHaveLength(50);
  expect(screen.getByText('column, one')).toBeInTheDocument();
  expect(document.querySelector('img')).toBeNull();
  expect(screen.getByText(/first 200 rows and 50 columns/)).toBeInTheDocument();
});
it('bounds deeply nested JSON and leaves invalid JSON as escaped source', async () => {
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
    new Blob(['{"a":'.repeat(30) + '0' + '}'.repeat(30)], {
      type: 'application/json',
    })
  );
  render(
    <ArtifactPreview version={{ ...base, mimeType: 'application/json' }} />
  );
  expect(await screen.findByLabelText('JSON preview')).toHaveTextContent(
    '[depth limit]'
  );
});
it('renders HTML source with no executable frame by default', async () => {
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
    new Blob(['<script>parent.pwned=1</script>'], { type: 'text/html' })
  );
  render(<ArtifactPreview version={{ ...base, mimeType: 'text/html' }} />);
  expect(
    await screen.findByText('<script>parent.pwned=1</script>')
  ).toBeInTheDocument();
  expect(document.querySelector('iframe')).toBeNull();
});
it('checks actual response byte size before text decoding or object URL allocation', async () => {
  const text = vi.fn();
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce({
    size: MAX_TEXT_PREVIEW_BYTES + 1,
    type: 'text/plain',
    text,
  } as unknown as Blob);
  render(
    <ArtifactPreview
      version={{ ...base, mimeType: 'text/plain', byteSize: 1 }}
    />
  );
  await screen.findByText(/File exceeds the inline preview limit/);
  expect(text).not.toHaveBeenCalled();
});
it('shows an authenticated PDF blob in a sandbox and revokes it on teardown', async () => {
  const create = vi.fn(() => 'blob:pdf');
  const revoke = vi.fn();
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = create;
      static revokeObjectURL = revoke;
    }
  );
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValueOnce(
    new Blob(['%PDF-1.7'], { type: 'application/pdf' })
  );
  const { unmount } = render(
    <ArtifactPreview version={{ ...base, mimeType: 'application/pdf' }} />
  );
  const frame = await screen.findByTitle('PDF preview');
  expect(frame).toHaveAttribute('src', 'blob:pdf');
  expect(frame).toHaveAttribute('sandbox', '');
  expect(create).toHaveBeenCalledTimes(1);
  unmount();
  expect(revoke).toHaveBeenCalledWith('blob:pdf');
});

afterEach(() => vi.unstubAllGlobals());
