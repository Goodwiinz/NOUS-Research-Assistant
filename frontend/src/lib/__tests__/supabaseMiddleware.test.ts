import { createServerClient } from '@supabase/ssr';
import { NextRequest } from 'next/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { updateSession } from '@/lib/supabase/middleware';

vi.mock('@supabase/ssr', () => ({
  createServerClient: vi.fn(),
}));

describe('updateSession auth refresh handoff', () => {
  // Mutation check (2026-09-14): removing the cookie-header synchronization
  // at src/lib/supabase/middleware.ts:56-61 makes the renderer receive
  // `stale-token` while the response still sets `refreshed-token`:
  // pnpm --dir frontend test src/lib/__tests__/supabaseMiddleware.test.ts --maxWorkers=2 --minWorkers=1
  beforeEach(() => {
    vi.mocked(createServerClient).mockReset();
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = 'test-anon-key';
  });

  it('forwards refreshed cookies to the renderer and browser without dropping request headers or cookie options', async () => {
    vi.mocked(createServerClient).mockImplementation(
      (_url, _key, options) =>
        ({
          auth: {
            getUser: async () => {
              options.cookies.setAll([
                {
                  name: 'sb-auth-token',
                  value: 'refreshed-token',
                  options: {
                    httpOnly: true,
                    path: '/',
                    sameSite: 'lax',
                    secure: true,
                  },
                },
              ]);
              return { data: { user: { id: 'user-1' } }, error: null };
            },
          },
        }) as never
    );
    const request = new NextRequest(
      'https://nous.example/chat?thread=thread-1',
      {
        headers: {
          cookie: 'sb-auth-token=stale-token; theme=dark',
          'x-nonce': 'nonce-1',
          'x-nous-request-path': '/chat?thread=thread-1',
        },
      }
    );
    const forwardedHeaders = new Headers(request.headers);

    const response = await updateSession(request, forwardedHeaders);

    expect(response.headers.get('x-middleware-request-cookie')).toBe(
      'sb-auth-token=refreshed-token; theme=dark'
    );
    expect(response.headers.get('x-middleware-request-x-nonce')).toBe(
      'nonce-1'
    );
    expect(
      response.headers.get('x-middleware-request-x-nous-request-path')
    ).toBe('/chat?thread=thread-1');
    expect(response.headers.get('set-cookie')).toContain(
      'sb-auth-token=refreshed-token'
    );
    expect(response.headers.get('set-cookie')).toContain('Path=/');
    expect(response.headers.get('set-cookie')).toContain('HttpOnly');
    expect(response.headers.get('set-cookie')).toContain('Secure');
    expect(response.headers.get('set-cookie')).toContain('SameSite=lax');
  });
});
