// @vitest-environment node
/**
 * Trigger.dev proxy routes: the runId path segment is validated before it is
 * interpolated into a Trigger API URL sent with TRIGGER_SECRET_KEY, and
 * upstream error bodies never reach the client.
 *
 * Before: `/api/v3/runs/${runId}` took the raw segment (an encoded `../`
 * reached other Trigger API paths) and failures returned
 * `Trigger.dev error: ${upstream text}` verbatim.
 */
import { NextRequest } from 'next/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const USER_ID = 'user-1';

vi.mock('@/lib/supabase/server', () => ({
  createClient: async () => ({
    auth: {
      getUser: async () => ({ data: { user: { id: USER_ID } }, error: null }),
      getSession: async () => ({
        data: { session: { access_token: 'tok' } },
        error: null,
      }),
    },
  }),
}));

const fetchMock = vi.fn();

async function loadRunRoute(): Promise<
  typeof import('../../../app/api/trigger/agent/runs/[runId]/route')
> {
  vi.resetModules();
  vi.stubEnv('TRIGGER_SECRET_KEY', 'tr_secret');
  return import('../../../app/api/trigger/agent/runs/[runId]/route');
}

async function loadConfirmRoute(): Promise<
  typeof import('../../../app/api/trigger/agent/runs/[runId]/confirm/route')
> {
  vi.resetModules();
  vi.stubEnv('TRIGGER_SECRET_KEY', 'tr_secret');
  return import('../../../app/api/trigger/agent/runs/[runId]/confirm/route');
}

function params(runId: string): { params: Promise<{ runId: string }> } {
  return { params: Promise.resolve({ runId }) };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('GET /api/trigger/agent/runs/[runId]', () => {
  it.each(['../../v1/projects', 'run_abc/../../x', 'not-a-run', '', 'run_'])(
    'rejects malformed runId %j without calling Trigger',
    async (runId) => {
      const { GET } = await loadRunRoute();
      const res = await GET(
        new NextRequest('http://localhost/api/trigger/agent/runs/x'),
        params(runId)
      );
      expect(res.status).toBe(404);
      expect(fetchMock).not.toHaveBeenCalled();
    }
  );

  it('fetches a well-formed run id', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run_abc123',
          status: 'COMPLETED',
          metadata: { userId: USER_ID },
        }),
        { status: 200 }
      )
    );
    const { GET } = await loadRunRoute();
    const res = await GET(
      new NextRequest('http://localhost/api/trigger/agent/runs/run_abc123'),
      params('run_abc123')
    );
    expect(res.status).toBe(200);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v3\/runs\/run_abc123$/);
  });

  it('does not echo upstream error text', async () => {
    fetchMock.mockResolvedValue(
      new Response('internal stack: secret-detail', { status: 500 })
    );
    const { GET } = await loadRunRoute();
    const res = await GET(
      new NextRequest('http://localhost/api/trigger/agent/runs/run_abc123'),
      params('run_abc123')
    );
    const body = await res.text();
    expect(res.status).toBe(502);
    expect(body).not.toContain('secret-detail');
  });

  it('maps an upstream 404 to the same Not found as a foreign run', async () => {
    fetchMock.mockResolvedValue(new Response('no such run', { status: 404 }));
    const { GET } = await loadRunRoute();
    const res = await GET(
      new NextRequest('http://localhost/api/trigger/agent/runs/run_abc123'),
      params('run_abc123')
    );
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: 'Not found' });
  });
});

describe('POST /api/trigger/agent/runs/[runId]/confirm', () => {
  function confirmRequest(): NextRequest {
    return new NextRequest(
      'http://localhost/api/trigger/agent/runs/x/confirm',
      {
        method: 'POST',
        body: JSON.stringify({ tokenId: 'waitpoint_1', confirmed: true }),
      }
    );
  }

  it('rejects a malformed runId without calling Trigger', async () => {
    const { POST } = await loadConfirmRoute();
    const res = await POST(confirmRequest(), params('../../v1/x'));
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not echo upstream error text when completing the token', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 'run_abc123',
            metadata: { userId: USER_ID, waitTokenId: 'waitpoint_1' },
          }),
          { status: 200 }
        )
      )
      .mockResolvedValueOnce(
        new Response('upstream says: secret-detail', { status: 500 })
      );
    const { POST } = await loadConfirmRoute();
    const res = await POST(confirmRequest(), params('run_abc123'));
    expect(res.status).toBe(502);
    expect(await res.text()).not.toContain('secret-detail');
  });
});
