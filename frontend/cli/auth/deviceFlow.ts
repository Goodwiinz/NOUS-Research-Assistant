import { saveConfig } from './store';
import { getApiBase, getDefaultApiBase } from '../services/client';

interface PollOptions {
  fetchFn?: typeof fetch;
  intervalMs?: number;
  apiBase?: string;
}

export interface ApprovalResult {
  token: string;
  user_email: string;
  organization_id: string;
  expires_at: string;
}

export interface CliAuthSession {
  session_id: string;
  poll_token: string;
  browser_url: string;
  verification_code: string;
  poll_interval_seconds: number;
}

export async function startCliAuth(
  apiBase: string = getApiBase()
): Promise<CliAuthSession> {
  const res = await fetch(`${apiBase}/cli-auth/start`, { method: 'POST' });
  if (!res.ok) throw new Error(`Failed to start CLI auth: ${res.status}`);
  return res.json();
}

/**
 * Start sign-in against the saved backend, falling back to the default one
 * when the saved host cannot be reached at all (a retired domain left in
 * config.json would otherwise make `./nous login` unrecoverable). An explicit
 * NOUS_API_URL is never second-guessed, and an HTTP error is not a fallback
 * trigger: that host is up and answered.
 */
async function startCliAuthWithFallback(): Promise<{
  apiBase: string;
  session: CliAuthSession;
}> {
  const saved = getApiBase();
  const fallback = getDefaultApiBase();
  try {
    return { apiBase: saved, session: await startCliAuth(saved) };
  } catch (err) {
    const unreachable = err instanceof TypeError;
    if (!unreachable || process.env.NOUS_API_URL || fallback === saved) {
      throw err;
    }
    console.log(
      `Saved backend ${saved} is unreachable; signing in to ${fallback} instead.`
    );
    return { apiBase: fallback, session: await startCliAuth(fallback) };
  }
}

export async function pollForApproval(
  session_id: string,
  poll_token: string,
  { fetchFn = fetch, intervalMs, apiBase = getApiBase() }: PollOptions = {}
): Promise<ApprovalResult> {
  const url = `${apiBase}/cli-auth/status/${encodeURIComponent(session_id)}`;

  while (true) {
    const res = await fetchFn(url, {
      headers: { 'X-CLI-Poll-Token': poll_token },
    });
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
    if (data.status === 'denied') {
      throw new Error('Login request denied. Run ./nous login again.');
    }
    if (data.status === 'cancelled') {
      throw new Error('Login cancelled. Run ./nous login again.');
    }
    if (data.status !== 'pending') {
      throw new Error('Unexpected login status. Run ./nous login again.');
    }

    const delay = intervalMs ?? 2000;
    if (delay > 0) await new Promise((r) => setTimeout(r, delay));
  }
}

export async function login(): Promise<void> {
  const { apiBase, session } = await startCliAuthWithFallback();
  const { session_id, poll_token, browser_url, verification_code } = session;

  // GOO-403: the link no longer carries the code. The user types it on the
  // approval page, which proves the person approving can see this terminal.
  console.log(`\nYour sign-in code: ${verification_code}`);
  console.log(
    'Type this code on the browser page. Only approve a request you started here.'
  );

  const { default: open } = await import('open');
  await open(browser_url);

  console.log('\nOpening browser for authorization...');
  console.log(`If browser did not open, visit:\n  ${browser_url}\n`);

  const result = await pollForApproval(session_id, poll_token, { apiBase });

  saveConfig({
    token: result.token,
    user_email: result.user_email,
    organization_id: result.organization_id,
    expires_at: result.expires_at,
    thread_id: null,
    api_url: apiBase,
  });

  console.log(`\n✓ Logged in as ${result.user_email}`);
}

export function isTokenExpired(expiresAt: string): boolean {
  return new Date(expiresAt).getTime() < Date.now();
}
