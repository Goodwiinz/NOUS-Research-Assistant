/** SSR/proxy auth must use configured Supabase endpoints, just like the browser. */
export function getSupabaseServerConfig(): {
  supabaseUrl: string;
  supabaseAnonKey: string;
} {
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!supabaseAnonKey) {
    throw new Error('NEXT_PUBLIC_SUPABASE_ANON_KEY is not configured.');
  }
  const supabaseUrl =
    process.env.SUPABASE_SERVER_URL?.trim() ||
    process.env.NEXT_PUBLIC_SUPABASE_URL?.trim();
  if (!supabaseUrl) {
    throw new Error(
      'SUPABASE_SERVER_URL or NEXT_PUBLIC_SUPABASE_URL is not configured.'
    );
  }
  return { supabaseUrl, supabaseAnonKey };
}
