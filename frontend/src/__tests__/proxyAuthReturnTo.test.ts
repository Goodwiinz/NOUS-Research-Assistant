import { NextRequest, NextResponse } from 'next/server';
import { describe, expect, it, vi } from 'vitest';

const middlewareMocks = vi.hoisted(() => ({
  updateSession: vi.fn(async (request: NextRequest, requestHeaders?: Headers) =>
    NextResponse.next({
      request: { headers: requestHeaders ?? request.headers },
    })
  ),
}));

vi.mock('@/lib/supabase/middleware', () => ({
  updateSession: middlewareMocks.updateSession,
}));

import { proxy } from '../../proxy';

describe('proxy protected-route recovery metadata', () => {
  it('forwards the requested path and query alongside CSP headers', async () => {
    const response = await proxy(
      new NextRequest(
        'https://nous.example/chat?thread=thread-1&panel=sources',
        {
          headers: {
            'x-client-header': 'preserved',
            'x-nous-request-path': '//evil.example/phish',
          },
        }
      )
    );

    expect(
      response.headers.get('x-middleware-request-x-nous-request-path')
    ).toBe('/chat?thread=thread-1&panel=sources');
    expect(response.headers.get('x-middleware-request-x-client-header')).toBe(
      'preserved'
    );
    expect(response.headers.get('x-middleware-request-x-nonce')).toBeTruthy();
    expect(response.headers.get('content-security-policy')).toContain(
      "script-src 'self' 'nonce-"
    );
  });
});
