import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * URL resolution contract for every APIClient transport (request, upload,
 * download, downloadPost, fetchObjectUrl): absolute and `/api/...` URLs pass
 * through untouched, other paths are joined onto baseURL, and a path without
 * a leading slash is a bug. It used to be silently glued onto the base
 * (`/api/v1knowledge-graph/health` -> 404), so it now fails loudly outside
 * production and is normalised (with a warning) in production.
 */
describe('APIClient URL resolution', () => {
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
    vi.unstubAllEnvs();
  });

  function stubJsonFetch(): ReturnType<typeof vi.fn> {
    // A fresh Response per call: a body can only be read once.
    const fetchMock = vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
    );
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  type APIClient = InstanceType<typeof import('../api-client').APIClient>;

  async function client(): Promise<APIClient> {
    const { APIClient } = await import('../api-client');
    return new APIClient('http://api.test/api/v1');
  }

  it('joins a leading-slash path onto baseURL', async () => {
    const fetchMock = stubJsonFetch();
    await (await client()).get('/knowledge-graph/health');
    expect(fetchMock.mock.calls[0][0]).toBe(
      'http://api.test/api/v1/knowledge-graph/health'
    );
  });

  it('passes absolute and /api/ URLs through unchanged', async () => {
    const fetchMock = stubJsonFetch();
    const api = await client();
    await api.get('https://other.test/x');
    await api.get('/api/v2/thing');
    expect(fetchMock.mock.calls[0][0]).toBe('https://other.test/x');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v2/thing');
  });

  it('treats /api/ URLs the same in download as in request', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/csv' }),
      blob: async () => new Blob(['a,b']),
    });
    vi.stubGlobal('fetch', fetchMock);
    const link = { href: '', download: '', click: vi.fn() };
    vi.spyOn(document, 'createElement').mockReturnValue(
      link as unknown as HTMLAnchorElement
    );
    vi.spyOn(document.body, 'appendChild').mockImplementation((n) => n);
    vi.spyOn(document.body, 'removeChild').mockImplementation((n) => n);
    Object.defineProperty(window.URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn().mockReturnValue('blob:x'),
    });
    Object.defineProperty(window.URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });

    await (await client()).download('/api/v2/export.csv', 'e.csv');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v2/export.csv');
  });

  it('throws on a path without a leading slash outside production', async () => {
    const fetchMock = stubJsonFetch();
    await expect(
      (await client()).get('knowledge-graph/health')
    ).rejects.toThrow(/leading slash/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('normalises a missing leading slash in production and warns', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const fetchMock = stubJsonFetch();
    await (await client()).get('knowledge-graph/health');
    expect(fetchMock.mock.calls[0][0]).toBe(
      'http://api.test/api/v1/knowledge-graph/health'
    );
    expect(warn).toHaveBeenCalled();
  });
});
