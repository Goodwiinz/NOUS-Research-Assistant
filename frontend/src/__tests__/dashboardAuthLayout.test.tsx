import { beforeEach, describe, expect, it, vi } from 'vitest';

const authLayoutMocks = vi.hoisted(() => ({
  createClient: vi.fn(),
  headers: vi.fn(),
  redirect: vi.fn(),
}));

vi.mock('@/lib/supabase/server', () => ({
  createClient: authLayoutMocks.createClient,
}));

vi.mock('next/headers', () => ({
  headers: authLayoutMocks.headers,
}));

vi.mock('next/navigation', () => ({
  redirect: authLayoutMocks.redirect,
}));

vi.mock('../../app/(dashboard)/dashboard-layout-client', () => ({
  default: ({ children }: { children: React.ReactNode }) => children,
}));

import DashboardLayout from '../../app/(dashboard)/layout';

describe('dashboard auth guard recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authLayoutMocks.headers.mockResolvedValue(
      new Headers({
        'x-nous-request-path': '/chat?thread=thread-1&panel=sources',
      })
    );
  });

  it('sends an anonymous user to login with the selected thread as next', async () => {
    authLayoutMocks.createClient.mockResolvedValue({
      auth: {
        getUser: vi.fn().mockResolvedValue({
          data: { user: null },
          error: null,
        }),
      },
    });

    await DashboardLayout({ children: null });

    expect(authLayoutMocks.redirect).toHaveBeenCalledWith(
      '/login?next=%2Fchat%3Fthread%3Dthread-1%26panel%3Dsources'
    );
  });

  it.each([null, { id: 'untrusted-user-payload' }])(
    'redirects a rejected 401 session without losing the selected thread (user=%j)',
    async (user) => {
      authLayoutMocks.createClient.mockResolvedValue({
        auth: {
          getUser: vi.fn().mockResolvedValue({
            data: { user },
            error: Object.assign(new Error('invalid token'), {
              name: 'AuthApiError',
              status: 401,
            }),
          }),
        },
      });

      await DashboardLayout({ children: null });

      expect(authLayoutMocks.redirect).toHaveBeenCalledWith(
        '/login?next=%2Fchat%3Fthread%3Dthread-1%26panel%3Dsources'
      );
    }
  );

  it.each([null, { id: 'untrusted-user-payload' }])(
    'fails closed before returning protected children on a transient verification error (user=%j)',
    async (user) => {
      const verificationError = Object.assign(
        new Error('auth service unavailable'),
        {
          name: 'AuthRetryableFetchError',
          status: 503,
        }
      );
      authLayoutMocks.createClient.mockResolvedValue({
        auth: {
          getUser: vi.fn().mockResolvedValue({
            data: { user },
            error: verificationError,
          }),
        },
      });

      await expect(DashboardLayout({ children: null })).rejects.toBe(
        verificationError
      );
      expect(authLayoutMocks.redirect).not.toHaveBeenCalled();
    }
  );
});
