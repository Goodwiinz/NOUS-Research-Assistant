import { createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';
import { authCookieOptions } from './cookieOptions';

/**
 * Refresh the Supabase session (SSR cookie auth) from the proxy.
 *
 * `requestHeaders` (optional) lets the caller forward MUTATED request headers
 * to Next's renderer via `NextResponse.next({ request: { headers } })` — the
 * documented v16 proxy "Setting Headers" pattern. proxy.ts uses this to pass
 * the per-request CSP nonce so Next applies it to its own framework scripts.
 * Omitting it preserves the previous behavior exactly.
 */
export async function updateSession(
  request: NextRequest,
  requestHeaders?: Headers
) {
  const forwardedHeaders = requestHeaders ?? request.headers;
  const nextInit = {
    request: { headers: forwardedHeaders },
  };
  let supabaseResponse = NextResponse.next(nextInit);

  // Server-side (proxy runtime). Prefer the server-only in-network URL when set
  // (containerized e2e) — the public URL is a browser host-port unreachable
  // from inside the container. Falls back to the public URL in production.
  const supabaseUrl =
    process.env.SUPABASE_SERVER_URL ||
    process.env.NEXT_PUBLIC_SUPABASE_URL ||
    'http://localhost:54321';
  // Fail fast instead of an empty key that 401s every request and masquerades
  // as a working auth guard (mirrors the throw in client.ts).
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!supabaseAnonKey) {
    throw new Error('NEXT_PUBLIC_SUPABASE_ANON_KEY is not configured.');
  }

  const supabase = createServerClient(supabaseUrl, supabaseAnonKey, {
    cookieOptions: authCookieOptions(),
    cookies: {
      getAll() {
        return request.cookies.getAll().map(({ name, value }) => ({
          name,
          value,
        }));
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value }) =>
          request.cookies.set(name, value)
        );
        // RequestCookies writes through to request.headers, but proxy.ts hands
        // us a separate Headers clone so CSP and other request metadata reach
        // the renderer. Keep that clone's Cookie header in lockstep too;
        // otherwise this response gives the browser refreshed cookies while
        // the current RSC render still authenticates with the stale token.
        const refreshedCookieHeader = request.headers.get('cookie');
        if (refreshedCookieHeader === null) {
          forwardedHeaders.delete('cookie');
        } else {
          forwardedHeaders.set('cookie', refreshedCookieHeader);
        }
        supabaseResponse = NextResponse.next(nextInit);
        cookiesToSet.forEach(({ name, value, options }) =>
          supabaseResponse.cookies.set(name, value, options)
        );
      },
    },
  });

  // Refresh the session — this is required for SSR cookie auth. Log a
  // verification/network error so a failing getUser() (unreachable auth
  // endpoint, bad key, GoTrue 5xx) is distinguishable from a real anon visitor.
  const { error } = await supabase.auth.getUser();
  if (error) {
    console.error(
      '[updateSession] getUser failed:',
      error.status,
      error.message
    );
  }

  return supabaseResponse;
}
