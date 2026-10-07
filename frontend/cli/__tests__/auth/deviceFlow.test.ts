// frontend/cli/__tests__/auth/deviceFlow.test.ts
import { expect, test, vi } from 'vitest';
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
});

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
