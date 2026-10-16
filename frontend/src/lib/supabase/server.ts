import { createServerClient } from '@supabase/ssr';
import type { SupabaseClient } from '@supabase/supabase-js';
import { cookies } from 'next/headers';
import { authCookieOptions } from './cookieOptions';
import { getSupabaseServerConfig } from './serverConfig';

export async function createClient(): Promise<SupabaseClient> {
  const cookieStore = await cookies();
  // NEXT_PUBLIC_SUPABASE_URL is a BROWSER URL inlined at build time. In a
  // containerized deploy (e.g. the e2e stack) that URL is a host-published port
  // like http://localhost:8999 which is unreachable from INSIDE the server
  // container, so getUser() fails and every authed route redirects to /login.
  // SUPABASE_SERVER_URL is an optional server-only runtime override pointing at
  // the in-network address (docker DNS). Unset in production, where the public
  // URL is reachable from the server too — so behavior is unchanged.
  const { supabaseUrl, supabaseAnonKey } = getSupabaseServerConfig();

  return createServerClient(supabaseUrl, supabaseAnonKey, {
    cookieOptions: authCookieOptions(),
    cookies: {
      getAll() {
        return cookieStore.getAll().map(({ name, value }) => ({ name, value }));
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value, options }) => {
          cookieStore.set(name, value, options);
        });
      },
    },
  });
}
