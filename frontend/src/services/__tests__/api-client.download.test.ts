import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('APIClient export downloads', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.doMock('@/lib/supabase/client', () => ({
      createClient: () => ({
        auth: {
          getSession: vi.fn().mockResolvedValue({ data: { session: null } }),
        },
      }),
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubDownloadDom(): {
    link: HTMLAnchorElement;
    click: ReturnType<typeof vi.fn>;
  } {
    const click = vi.fn();
    const link = {
      href: '',
      download: '',
      click,
    } as unknown as HTMLAnchorElement;
    vi.spyOn(document, 'createElement').mockReturnValue(link);
    vi.spyOn(document.body, 'appendChild').mockImplementation((node) => node);
    vi.spyOn(document.body, 'removeChild').mockImplementation((node) => node);
    Object.defineProperty(window.URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn().mockReturnValue('blob:export'),
    });
    Object.defineProperty(window.URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
    return { link, click };
  }

  it('prefers the server Content-Disposition filename over a caller guess', async () => {
    const { link, click } = stubDownloadDom();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers({
          'content-type': 'application/pdf',
          'content-disposition': 'attachment; filename="server.pdf"',
        }),
        blob: async () => new Blob(['%PDF-1.7'], { type: 'application/pdf' }),
      })
    );

    const { APIClient } = await import('../api-client');
    const client = new APIClient('http://api.test');

    await client.downloadPost('/export', 'guessed.pdf', undefined, {
      contentType: 'application/pdf',
      signature: '%PDF-',
    });

    expect(link.download).toBe('server.pdf');
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('rejects an HTML body or MIME type when a PDF was requested', async () => {
    const { click } = stubDownloadDom();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers({
          'content-type': 'text/html; charset=utf-8',
          'content-disposition': 'attachment; filename="wrong.pdf"',
        }),
        blob: async () => new Blob(['<!DOCTYPE html>'], { type: 'text/html' }),
      })
    );

    const { APIClient } = await import('../api-client');
    const client = new APIClient('http://api.test');

    await expect(
      client.downloadPost('/export', undefined, undefined, {
        contentType: 'application/pdf',
        signature: '%PDF-',
      })
    ).rejects.toMatchObject({
      name: 'APIError',
      message: 'Export response did not match the requested format',
    });
    expect(click).not.toHaveBeenCalled();
  });

  it('rejects an HTML body even when its MIME claims to be PDF', async () => {
    const { click } = stubDownloadDom();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers({
          'content-type': 'application/pdf',
          'content-disposition': 'attachment; filename="wrong.pdf"',
        }),
        blob: async () =>
          new Blob(['<!DOCTYPE html>'], { type: 'application/pdf' }),
      })
    );

    const { APIClient } = await import('../api-client');
    const client = new APIClient('http://api.test');

    await expect(
      client.downloadPost('/export', undefined, undefined, {
        contentType: 'application/pdf',
        signature: '%PDF-',
      })
    ).rejects.toMatchObject({
      name: 'APIError',
      message: 'Export response did not match the requested format',
    });
    expect(click).not.toHaveBeenCalled();
  });
});
