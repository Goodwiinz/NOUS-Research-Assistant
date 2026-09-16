import { expect, test, vi } from 'vitest';
import type { MockedFunction } from 'vitest';
import {
  getApiBase,
  getCliAuthHeaders,
  safeFetch,
} from '../../services/client';

vi.mock('../../auth/store');

import { loadConfig } from '../../auth/store';

const mockLoadConfig = loadConfig as MockedFunction<typeof loadConfig>;

test('returns Authorization header when token is present', () => {
  mockLoadConfig.mockReturnValue({
    token: 'tok_test',
    user_email: 'x@y.com',
    organization_id: 'org_1',
    expires_at: '2099-01-01T00:00:00Z',
    thread_id: null,
  });
  const headers = getCliAuthHeaders();
  expect(headers['Authorization']).toBe('Bearer tok_test');
  // The backend derives org from the authenticated user; the dead
  // X-Organization-ID header must not be sent.
  expect(headers['X-Organization-ID']).toBeUndefined();
});

test('throws when not logged in', () => {
  mockLoadConfig.mockReturnValue(null);
  expect(() => getCliAuthHeaders()).toThrow('Not logged in');
});

test('safeFetch wraps fetch failures with the URL in the message', async () => {
  const inner = Object.assign(new Error('connect ECONNREFUSED'), {
    code: 'ECONNREFUSED',
  });
  const fetchFn = vi.fn().mockRejectedValue(inner);
  await expect(
    safeFetch('http://example.test/x', undefined, fetchFn as never)
  ).rejects.toMatchObject({
    message: expect.stringContaining('http://example.test/x'),
    code: 'ECONNREFUSED',
  });
});

test('explicit backend overrides saved settings, which override the launcher default', () => {
  const api = process.env.NOUS_API_URL;
  const fallback = process.env.NOUS_DEFAULT_API_URL;
  try {
    process.env.NOUS_DEFAULT_API_URL = 'https://default.invalid/api/v1';
    delete process.env.NOUS_API_URL;
    mockLoadConfig.mockReturnValue(null);
    expect(getApiBase()).toBe(process.env.NOUS_DEFAULT_API_URL);
    mockLoadConfig.mockReturnValue({
      api_url: 'https://saved.invalid/api/v1',
    } as ReturnType<typeof loadConfig>);
    expect(getApiBase()).toBe('https://saved.invalid/api/v1');
    process.env.NOUS_API_URL = 'https://override.invalid/api/v1';
    expect(getApiBase()).toBe(process.env.NOUS_API_URL);
  } finally {
    if (api === undefined) delete process.env.NOUS_API_URL;
    else process.env.NOUS_API_URL = api;
    if (fallback === undefined) delete process.env.NOUS_DEFAULT_API_URL;
    else process.env.NOUS_DEFAULT_API_URL = fallback;
  }
});
