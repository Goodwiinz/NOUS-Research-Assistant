import { beforeEach, describe, expect, it, vi } from 'vitest';

import { exportThread } from '@/services/export-service';

const downloadPost = vi.fn();
vi.mock('@/services/api-client', () => ({
  api: {
    downloadPost: (...args: unknown[]) => downloadPost(...args),
  },
}));

describe('exportThread', () => {
  beforeEach(() => {
    downloadPost.mockReset();
  });

  // Regression: api-client's baseURL already ends in /api/v1. Prefixing it
  // again produced POST /api/v1/api/v1/export/... -> 404, surfaced in the UI
  // as the generic "Export failed. Please try again." toast.
  it('requests a path relative to the api-client base, not a second /api/v1', async () => {
    await exportThread('thread-1', 'json');

    const [url, filename] = downloadPost.mock.calls[0] as [string, string?];
    expect(url).toMatch(/^\/export\/thread\/thread-1\?/);
    expect(url).not.toContain('/api/v1');
    expect(filename).toBeUndefined();
  });

  it('asks the binary downloader to validate a PDF response and use its server filename', async () => {
    await exportThread('thread-1', 'pdf');

    const [url, filename, body, expectation] = downloadPost.mock.calls[0] as [
      string,
      string | undefined,
      unknown,
      { contentType: string; signature: string },
    ];
    expect(url).toMatch(/^\/export\/thread\/thread-1\?/);
    expect(filename).toBeUndefined();
    expect(body).toBeUndefined();
    expect(expectation).toEqual({
      contentType: 'application/pdf',
      signature: '%PDF-',
    });
  });
});

describe('exportBatch', () => {
  beforeEach(() => {
    downloadPost.mockReset();
  });

  it('uses the authenticated binary POST path for a non-ZIP single export', async () => {
    const { exportBatch } = await import('@/services/export-service');

    await exportBatch({
      threadIds: ['thread-1'],
      format: 'pdf',
      asZip: false,
    });

    expect(downloadPost).toHaveBeenCalledWith(
      '/export/batch',
      undefined,
      expect.objectContaining({
        thread_ids: ['thread-1'],
        format: 'pdf',
        as_zip: false,
      }),
      { contentType: 'application/pdf', signature: '%PDF-' }
    );
  });

  it('validates the ZIP response when batch packaging is requested', async () => {
    const { exportBatch } = await import('@/services/export-service');

    await exportBatch({
      threadIds: ['thread-1', 'thread-2'],
      format: 'markdown',
      asZip: true,
    });

    expect(downloadPost).toHaveBeenCalledWith(
      '/export/batch',
      undefined,
      expect.objectContaining({ as_zip: true }),
      { contentType: 'application/zip', signature: 'PK' }
    );
  });
});
