// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GET } from '../../../app/api/backend-capabilities/route';

const paths = {
  '/api/v1/projects/{project_id}/claims': { get: {} },
  '/api/v1/projects/{project_id}/claims/export': { get: {} },
  '/api/v1/projects/{project_id}/drafts/{draft_id}/versions/{version}/release':
    { get: {} },
  '/api/v1/projects/{project_id}/drafts/{draft_id}/versions/{version}/promote':
    { post: {} },
  '/api/v1/research-engine/projects/{project_id}/roles': { get: {} },
};

describe('deployed backend capabilities', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubEnv('BACKEND_URL', 'https://server.example.com/api/v1');
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });
  it('uses the configured backend and requires all operations for each panel', async () => {
    fetchMock.mockResolvedValue(Response.json({ paths }));
    const response = await GET();
    expect(await response.json()).toEqual({
      draftClaims: true,
      draftRelease: true,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://server.example.com/openapi.json',
      expect.objectContaining({ cache: 'no-store' })
    );
    expect(response.headers.get('Cache-Control')).toContain('no-store');
  });
  it('disables controls on an older backend', async () => {
    fetchMock.mockResolvedValue(Response.json({ paths: {} }));
    expect(await (await GET()).json()).toEqual({
      draftClaims: false,
      draftRelease: false,
    });
  });
  it('does not enable promotion when only the release read exists', async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        paths: {
          '/api/v1/projects/{project_id}/drafts/{draft_id}/versions/{version}/release':
            { get: {} },
        },
      })
    );
    expect((await (await GET()).json()).draftRelease).toBe(false);
  });
  it.each([null, { paths: [] }, { paths: 'invalid' }])(
    'fails closed on malformed schema %s',
    async (schema) => {
      fetchMock.mockResolvedValue(Response.json(schema));
      expect((await GET()).status).toBe(503);
    }
  );
  it('fails closed when the schema probe fails', async () => {
    fetchMock.mockRejectedValue(new Error('internal connection information'));
    const response = await GET();
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      draftClaims: false,
      draftRelease: false,
    });
  });
});
