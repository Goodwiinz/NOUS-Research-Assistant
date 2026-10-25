// frontend/cli/__tests__/auth/deviceFlow.test.ts
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { pollForApproval } from '../../auth/deviceFlow';

test('resolves with token when status becomes approved', async () => {
  let callCount = 0;
  const mockFetch = vi.fn().mockImplementation(() => {
    callCount++;
    const status = callCount >= 3 ? 'approved' : 'pending';
    const extra =
      status === 'approved'
        ? {
            token: 'tok_approved',
            user_email: 'a@b.com',
            organization_id: 'org1',
            expires_at: '2099-01-01T00:00:00Z',
          }
        : {};
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ status, ...extra }),
    });
  });

  const result = await pollForApproval('sess_1', 'pt_1', {
    fetchFn: mockFetch as any,
    intervalMs: 0,
  });
  expect(result.token).toBe('tok_approved');
  expect(callCount).toBe(3);
});

test('rejects when status is expired', async () => {
  const mockFetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ status: 'expired' }),
  });
  await expect(
    pollForApproval('sess_1', 'pt_1', {
      fetchFn: mockFetch as any,
      intervalMs: 0,
    })
  ).rejects.toThrow('expired');
  expect(mockFetch).toHaveBeenCalledTimes(1);
});

// Mutation-verified guard: frontend/cli/auth/deviceFlow.ts:55-63.
// Removing the non-pending terminal checks makes all three cases consume a token.
// pnpm --dir frontend exec vitest run cli/__tests__/auth/deviceFlow.test.ts
test.each(['denied', 'cancelled', 'unexpected-status'])(
  'stops polling immediately for %s without consuming a later token',
  async (status) => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status }),
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({ status: 'approved', token: 'must-not-consume' }),
      });
    await expect(
      pollForApproval('sess_1', 'pt_1', {
        fetchFn: mockFetch,
        intervalMs: 0,
      })
    ).rejects.toThrow(/denied|cancelled|unexpected login status/i);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  }
);

test('sends the poll token in a header, never in the URL', async () => {
  const mockFetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ status: 'approved', token: 't' }),
  });
  await pollForApproval('sess_1', 'pt_secret', {
    fetchFn: mockFetch as any,
    intervalMs: 0,
  });
  const [url, init] = mockFetch.mock.calls[0];
  expect(String(url)).not.toContain('pt_secret');
  expect(init.headers['X-CLI-Poll-Token']).toBe('pt_secret');
});

test('login prints the verification code for the user to type in the browser', async () => {
  vi.resetModules();
  vi.doMock('open', () => ({ default: vi.fn() }));
  vi.doMock('../../auth/store', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../../auth/store')>()),
    saveConfig: vi.fn(),
  }));
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          session_id: 's',
          poll_token: 'p',
          browser_url: 'https://nous.test/cli-auth?session_id=s',
          verification_code: 'ABCD-1234',
          poll_interval_seconds: 0,
        }),
    })
    .mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          status: 'approved',
          token: 't',
          user_email: 'a@b.com',
          organization_id: 'o',
          expires_at: '2099-01-01T00:00:00Z',
        }),
    });
  vi.stubGlobal('fetch', fetchMock);
  const log = vi.spyOn(console, 'log').mockImplementation(() => {});
  try {
    const { login } = await import('../../auth/deviceFlow');
    await login();
    const printed = log.mock.calls.flat().join('\n');
    expect(printed).toContain('ABCD-1234');
    expect(printed).toMatch(/type this code/i);
  } finally {
    log.mockRestore();
    vi.unstubAllGlobals();
    vi.doUnmock('open');
    vi.doUnmock('../../auth/store');
  }
});

describe('login backend fallback', () => {
  const DEAD = 'https://dead.test/api/v1';
  const DEV = 'https://dev.test/api/v1';
  const startOk = {
    ok: true,
    json: () =>
      Promise.resolve({
        session_id: 's',
        poll_token: 'p',
        browser_url: 'https://nous.test/cli-auth?session_id=s',
        verification_code: 'ABCD-1234',
        poll_interval_seconds: 0,
      }),
  };
  const approved = {
    ok: true,
    json: () =>
      Promise.resolve({
        status: 'approved',
        token: 't',
        user_email: 'a@b.com',
        organization_id: 'o',
        expires_at: '2099-01-01T00:00:00Z',
      }),
  };

  async function loadLogin(savedApiUrl: string) {
    vi.resetModules();
    const saveConfig = vi.fn();
    vi.doMock('open', () => ({ default: vi.fn() }));
    vi.doMock('../../auth/store', async (importOriginal) => ({
      ...(await importOriginal<typeof import('../../auth/store')>()),
      loadConfig: () => ({
        token: 'old',
        user_email: 'a@b.com',
        organization_id: 'o',
        expires_at: '2000-01-01T00:00:00Z',
        thread_id: null,
        api_url: savedApiUrl,
      }),
      saveConfig,
    }));
    const { login } = await import('../../auth/deviceFlow');
    return { login, saveConfig };
  }

  let log: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    vi.stubEnv('NOUS_API_URL', '');
    vi.stubEnv('NOUS_DEFAULT_API_URL', DEV);
    log = vi.spyOn(console, 'log').mockImplementation(() => {});
  });
  afterEach(() => {
    log.mockRestore();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.doUnmock('open');
    vi.doUnmock('../../auth/store');
  });

  test('signs in to the default API when the saved api_url is unreachable', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('fetch failed'))
      .mockResolvedValueOnce(startOk)
      .mockResolvedValueOnce(approved);
    vi.stubGlobal('fetch', fetchMock);
    const { login, saveConfig } = await loadLogin(DEAD);

    await login();

    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls[0]).toBe(`${DEAD}/cli-auth/start`);
    expect(urls[1]).toBe(`${DEV}/cli-auth/start`);
    expect(urls[2].startsWith(`${DEV}/cli-auth/status/`)).toBe(true);
    expect(saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ api_url: DEV })
    );
    const printed = log.mock.calls.flat().join('\n');
    expect(printed).toContain(DEAD);
    expect(printed).toContain(DEV);
  });

  test('does not fall back when NOUS_API_URL is set explicitly', async () => {
    vi.stubEnv('NOUS_API_URL', DEAD);
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    vi.stubGlobal('fetch', fetchMock);
    const { login } = await loadLogin(DEAD);

    await expect(login()).rejects.toThrow('fetch failed');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test('does not fall back when the saved backend answers with an HTTP error', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    vi.stubGlobal('fetch', fetchMock);
    const { login } = await loadLogin(DEAD);

    await expect(login()).rejects.toThrow('Failed to start CLI auth: 500');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
