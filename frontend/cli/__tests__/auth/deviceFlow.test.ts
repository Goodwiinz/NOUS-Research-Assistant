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
