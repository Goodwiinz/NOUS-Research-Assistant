import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getSupabaseServerConfig } from '../serverConfig';

describe('server Supabase endpoint configuration', () => {
  beforeEach(() => {
    vi.stubEnv('SUPABASE_SERVER_URL', '');
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', '');
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_ANON_KEY', 'test-anon-key');
  });
  afterEach(() => vi.unstubAllEnvs());

  it('fails clearly when the endpoint is missing instead of contacting localhost', () => {
    expect(getSupabaseServerConfig).toThrow(
      /SUPABASE_SERVER_URL or NEXT_PUBLIC_SUPABASE_URL/
    );
  });

  it('keeps the configured browser endpoint when no server override is needed', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', 'https://auth.example');
    expect(getSupabaseServerConfig()).toEqual({
      supabaseUrl: 'https://auth.example',
      supabaseAnonKey: 'test-anon-key',
    });
  });

  it('prefers the explicit in-network server override for containerized auth', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', 'http://localhost:8999');
    vi.stubEnv('SUPABASE_SERVER_URL', 'http://supabase-kong:8000');
    expect(getSupabaseServerConfig().supabaseUrl).toBe(
      'http://supabase-kong:8000'
    );
  });

  it('supports a deliberately configured local auth endpoint', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', 'http://127.0.0.1:54321');
    expect(getSupabaseServerConfig().supabaseUrl).toBe(
      'http://127.0.0.1:54321'
    );
  });

  it('continues to require a genuine configured anon key', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', 'https://auth.example');
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_ANON_KEY', '');
    expect(getSupabaseServerConfig).toThrow(/ANON_KEY is not configured/);
  });
});
