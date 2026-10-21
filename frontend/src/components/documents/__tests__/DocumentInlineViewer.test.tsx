import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DocumentInlineViewer } from '../DocumentInlineViewer';
import { api } from '@/services/api-client';
import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';
import { APIErrorClass } from '@/types';

vi.mock('@/services/api-client', () => ({
  api: { fetchBlob: vi.fn(), download: vi.fn() },
}));
vi.mock('../PdfDocumentViewer', () => ({
  PdfDocumentViewer: ({
    title,
    targetPage,
    documentId,
    fallbackUrl,
  }: {
    title: string;
    targetPage?: number;
    documentId?: string;
    fallbackUrl?: string;
  }) => (
    <div
      aria-label="Controlled PDF"
      data-document-id={documentId}
      data-fallback-url={fallbackUrl}
    >
      {title}, page {targetPage ?? 1}
    </div>
  ),
}));

beforeEach(() => {
  let next = 0;
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = vi.fn(() => `blob:pdf-${++next}`);
      static revokeObjectURL = vi.fn();
    }
  );
  vi.mocked(api.fetchBlob).mockResolvedValue(new Blob(['%PDF']));
  useAuthStore.setState({
    user: { id: 'owner-a', organization_id: 'org-a' } as User,
    isAuthenticated: true,
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('document PDF identity and navigation', () => {
  it('passes one-based citation pages to the controlled viewer and new-tab URL', async () => {
    const props = { documentId: 'doc-1', filename: 'paper.pdf', kind: 'pdf' };
    const { rerender } = render(
      <DocumentInlineViewer {...props} targetPage={3} />
    );
    await screen.findByText('paper.pdf, page 3');
    expect(
      screen.getByRole('link', { name: 'Open in new tab' })
    ).toHaveAttribute('href', 'blob:pdf-1#page=3');
    // The read-only fallback reuses the same object URL and reports the id.
    expect(screen.getByLabelText('Controlled PDF')).toHaveAttribute(
      'data-fallback-url',
      'blob:pdf-1'
    );
    expect(screen.getByLabelText('Controlled PDF')).toHaveAttribute(
      'data-document-id',
      'doc-1'
    );
    rerender(<DocumentInlineViewer {...props} targetPage={1} />);
    expect(screen.getByText('paper.pdf, page 1')).toBeInTheDocument();
    expect(api.fetchBlob).toHaveBeenCalledTimes(1);
    expect(api.fetchBlob).toHaveBeenCalledWith(
      '/files/doc-1/download',
      expect.any(AbortSignal)
    );
  });

  // Mutation verified: removing account/org/auth from DocumentInlineViewer.tsx:69
  // leaves the prior controlled viewer visible after switching accounts.
  // pnpm --dir frontend test src/components/documents/__tests__/DocumentInlineViewer.test.tsx -t 'aborts account-bound'
  it('aborts account-bound downloads, revokes prior URLs and hides old bytes synchronously', async () => {
    const props = {
      documentId: 'same-doc',
      filename: 'paper.pdf',
      kind: 'pdf',
    };
    render(<DocumentInlineViewer {...props} />);
    await screen.findByLabelText('Controlled PDF');
    const oldSignal = vi.mocked(api.fetchBlob).mock.calls[0][1]!;
    vi.mocked(api.fetchBlob).mockReturnValueOnce(new Promise(() => {}));
    act(() =>
      useAuthStore.setState({
        user: { id: 'owner-b', organization_id: 'org-b' } as User,
      })
    );
    expect(screen.queryByLabelText('Controlled PDF')).toBeNull();
    expect(oldSignal.aborted).toBe(true);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:pdf-1');
    await waitFor(() => expect(api.fetchBlob).toHaveBeenCalledTimes(2));
  });

  // Mutation verified: remove DocumentInlineViewer.tsx:100's cancelled check
  // at fetchBlob.then. This test observes an extra URL and lost new preview.
  // pnpm --dir frontend test src/components/documents/__tests__/DocumentInlineViewer.test.tsx -t 'discarded late'
  it('discarded late downloads never mint or install an object URL in a replacement document', async () => {
    let finish!: (blob: Blob) => void;
    vi.mocked(api.fetchBlob).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      })
    );
    const { rerender } = render(
      <DocumentInlineViewer documentId="old" filename="old.pdf" kind="pdf" />
    );
    rerender(
      <DocumentInlineViewer documentId="new" filename="new.pdf" kind="pdf" />
    );
    await screen.findByText('new.pdf, page 1');
    await act(async () => finish(new Blob(['late old bytes'])));
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('old.pdf, page 1')).toBeNull();
  });
});

describe('document preview download failures', () => {
  const apiError = (status: number): APIErrorClass =>
    new APIErrorClass({
      message: `raw server detail ${status}`,
      status_code: status,
      type: 'http_error',
    });

  it.each([
    [apiError(404), 'This file could not be found. It may have been deleted.'],
    [apiError(403), "You don't have access to this file."],
    [apiError(410), 'This file is no longer available.'],
    [
      new TypeError('Failed to fetch'),
      'Could not reach the server. Check your connection and try again.',
    ],
    [apiError(500), 'Could not load this file for preview.'],
  ])('shows a specific, safe message for %s', async (error, message) => {
    vi.mocked(api.fetchBlob).mockRejectedValueOnce(error);
    render(
      <DocumentInlineViewer documentId="doc" filename="paper.pdf" kind="pdf" />
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.getByRole('alert')).not.toHaveTextContent('raw server');
  });
});
