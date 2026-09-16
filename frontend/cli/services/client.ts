import { loadConfig } from '../auth/store';

export function getCliAuthHeaders(): Record<string, string> {
  const config = loadConfig();
  if (!config) throw new Error('Not logged in. Run: ./nous login');
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${config.token}`,
  };
}

const DEFAULT_API_BASE = 'http://localhost:8000/api/v1';

export function getApiBase(): string {
  if (process.env.NOUS_API_URL) return process.env.NOUS_API_URL;
  const config = loadConfig();
  if (config?.api_url) return config.api_url;
  return process.env.NOUS_DEFAULT_API_URL || DEFAULT_API_BASE;
}

export const API_BASE = getApiBase();

export async function safeFetch(
  url: string,
  init?: RequestInit,
  fetchFn: typeof fetch = fetch
): Promise<Response> {
  try {
    return await fetchFn(url, init);
  } catch (err) {
    const orig = err instanceof Error ? err.message : String(err);
    const wrapped = new Error(`fetch failed at ${url}: ${orig}`);
    if (err instanceof Error) {
      (wrapped as Error & { code?: string }).code = (
        err as Error & { code?: string }
      ).code;
      (wrapped as Error & { cause?: unknown }).cause = err;
    }
    throw wrapped;
  }
}
