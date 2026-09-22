import { saveConfig } from './store';
import { getApiBase } from '../services/client';

const BACKEND_URL = getApiBase();

interface PollOptions {
  fetchFn?: typeof fetch;
  intervalMs?: number;
}

export interface ApprovalResult {
  token: string;
  user_email: string;
  organization_id: string;
  expires_at: string;
}

export async function startCliAuth(): Promise<{
  session_id: string;
  poll_token: string;
  browser_url: string;
  poll_interval_seconds: number;
}> {
  const res = await fetch(`${BACKEND_URL}/cli-auth/start`, { method: 'POST' });
  if (!res.ok) throw new Error(`Failed to start CLI auth: ${res.status}`);
  return res.json();
}

export async function pollForApproval(
  session_id: string,
  poll_token: string,
  { fetchFn = fetch, intervalMs }: PollOptions = {}
): Promise<ApprovalResult> {
  const url = `${BACKEND_URL}/cli-auth/status/${encodeURIComponent(session_id)}?poll_token=${encodeURIComponent(poll_token)}`;

  while (true) {
    const res = await fetchFn(url);
    if (!res.ok) throw new Error(`Poll failed: ${res.status}`);
    const data = await res.json();

    if (data.status === 'approved') {
      return {
        token: data.token,
        user_email: data.user_email,
        organization_id: data.organization_id,
        expires_at: data.expires_at,
      };
    }
    if (data.status === 'expired') {
      throw new Error('Login session expired. Run ./nous login again.');
    }

    const delay = intervalMs ?? 2000;
    if (delay > 0) await new Promise((r) => setTimeout(r, delay));
  }
}

export async function login(): Promise<void> {
  const { session_id, poll_token, browser_url } = await startCliAuth();

  const { default: open } = await import('open');
  await open(browser_url);

  console.log('\nOpening browser for authorization...');
  console.log(`If browser did not open, visit:\n  ${browser_url}\n`);

  const result = await pollForApproval(session_id, poll_token);

  saveConfig({
    token: result.token,
    user_email: result.user_email,
    organization_id: result.organization_id,
    expires_at: result.expires_at,
    thread_id: null,
    api_url: BACKEND_URL,
  });

  console.log(`\n✓ Logged in as ${result.user_email}`);
}

export function isTokenExpired(expiresAt: string): boolean {
  return new Date(expiresAt).getTime() < Date.now();
}
