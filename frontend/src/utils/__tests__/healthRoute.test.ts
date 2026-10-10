// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GET } from '../../../app/api/health/route';

describe('frontend health backend configuration', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubEnv('BACKEND_URL', '');
    vi.stubEnv('NEXT_PUBLIC_API_URL', '');
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', '');
    vi.stubEnv('VERCEL', '1');
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(Response.json({ status: 'healthy' }));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it('probes BACKEND_URL when it is the only configured backend', async () => {
    vi.stubEnv('BACKEND_URL', 'https://dev-api.goodwiinz.tech/api/v1/');

    const response = await GET();

    expect(response.status).toBe(200);
    expect((await response.json()).backend.status).toBe('healthy');
    expect(fetchMock).toHaveBeenCalledWith(
      'https://dev-api.goodwiinz.tech/health',
      expect.objectContaining({ method: 'GET', cache: 'no-store' })
    );
  });

  it('prefers BACKEND_URL over the public API URL, like rewrites', async () => {
    vi.stubEnv('BACKEND_URL', 'https://server.example.com');
    vi.stubEnv('NEXT_PUBLIC_API_URL', 'https://public.example.com');

    await GET();

    expect(fetchMock).toHaveBeenCalledWith(
      'https://server.example.com/health',
      expect.any(Object)
    );
  });

  it('falls back to the public API URL', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_URL', 'https://public.example.com/api/v1');

    await GET();

    expect(fetchMock).toHaveBeenCalledWith(
      'https://public.example.com/health',
      expect.any(Object)
    );
  });

  it.each(['', 'not-a-url', 'http://localhost:8000'])(
    'keeps frontend liveness when backend configuration is unusable: %s',
    async (backendUrl) => {
      vi.stubEnv('BACKEND_URL', backendUrl);

      const response = await GET();

      expect(response.status).toBe(200);
      expect((await response.json()).backend.status).toBe('unknown');
      expect(fetchMock).not.toHaveBeenCalled();
    }
  );
});
