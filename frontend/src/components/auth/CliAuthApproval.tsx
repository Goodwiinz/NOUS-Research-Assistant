'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { cn } from '@/lib/utils';
import { api } from '@/services/api-client';
import { APIErrorClass } from '@/types/api';
import type { components } from '@/types/generated/api';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ShieldCheck,
} from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import React, { useEffect, useState } from 'react';

type CliAuthSessionInfo = components['schemas']['CLIAuthSessionInfo'];
type CliAuthApproveRequest = components['schemas']['CLIAuthApproveRequest'];

interface CliAuthApprovalProps {
  sessionId: string;
  linkCarriesCode: boolean;
  userId: string | null;
  accountRevision: number;
  isAuthenticated: boolean;
  isLoading: boolean;
}

const CODE_LENGTH = 8;

/** Uppercase, drop separators and cap at the code length. */
function normalizeCode(raw: string): string {
  return raw
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
    .slice(0, CODE_LENGTH);
}

/** Show `ABCD1234` as `ABCD-1234`, the way the terminal prints it. */
function formatCode(chars: string): string {
  return chars.length > 4 ? `${chars.slice(0, 4)}-${chars.slice(4)}` : chars;
}

function formatStartedAt(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function CliAuthApproval({
  sessionId,
  linkCarriesCode,
  userId,
  accountRevision,
  isAuthenticated,
  isLoading,
}: CliAuthApprovalProps): React.JSX.Element {
  const router = useRouter();
  const [codeChars, setCodeChars] = useState('');
  const queryClient = useQueryClient();
  const queryKey = [
    'cli-auth-session',
    userId,
    accountRevision,
    sessionId,
  ] as const;
  const request = useQuery({
    queryKey,
    enabled: Boolean(sessionId && userId && isAuthenticated),
    queryFn: ({ signal }) =>
      api.get<CliAuthSessionInfo>(
        `/cli-auth/session/${encodeURIComponent(sessionId)}`,
        { signal, retries: 0 }
      ),
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
  const approval = useMutation({
    mutationFn: (verificationCode: string) =>
      api.post('/cli-auth/approve', {
        session_id: sessionId,
        verification_code: verificationCode,
      } satisfies CliAuthApproveRequest),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey });
    },
  });
  const details = request.data;
  const isSubmitting = approval.isPending;
  const isConnected = approval.isSuccess;
  const approvalStatus =
    approval.error instanceof APIErrorClass
      ? approval.error.error.status_code
      : undefined;
  const sessionUnavailable = approvalStatus === 404;
  const error = !approval.isError
    ? ''
    : sessionUnavailable
      ? 'This sign-in request is no longer waiting for approval. Run the login command in your terminal again.'
      : approvalStatus === 400
        ? 'Verification code does not match the one shown in your terminal'
        : 'This sign-in request could not be approved. Run the login command in your terminal again.';

  // GOO-403 (RFC 8628 §5.4): a code carried by the link is never used. It may
  // come from an older backend or from a link someone else sent. Scrub it from
  // the address bar and drop any copy an older version of this page stored.
  useEffect(() => {
    if (window.location.hash || linkCarriesCode) {
      window.history.replaceState(
        null,
        '',
        `/cli-auth?session_id=${encodeURIComponent(sessionId)}`
      );
    }
    try {
      sessionStorage.removeItem(`nous:cli-auth-code:${sessionId}`);
    } catch {
      // Storage unavailable: nothing was stored.
    }
  }, [sessionId, linkCarriesCode]);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      const next = `/cli-auth?session_id=${encodeURIComponent(sessionId)}`;
      router.push(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [isAuthenticated, isLoading, router, sessionId]);

  // Derived during render (no set-state in an effect): a link without a
  // session id can never load details, so say so instead of "Loading…".
  const detailsError = !sessionId
    ? 'This link is missing its sign-in request. Run the login command in your terminal again.'
    : request.isError
      ? 'This sign-in request was not found or has expired. Run the login command in your terminal again.'
      : '';
  const isPending = details?.status === 'pending' && !sessionUnavailable;
  const canApprove =
    Boolean(sessionId) &&
    isPending &&
    !request.isError &&
    !request.isFetching &&
    codeChars.length === CODE_LENGTH &&
    !isSubmitting &&
    !isConnected;

  const handleApprove = (): void => {
    if (canApprove) approval.mutate(formatCode(codeChars));
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-6 py-12 text-foreground">
      <section className="w-full max-w-xl rounded-2xl border border-border bg-card p-8 shadow-lg">
        <div className="mb-7 flex items-start gap-4">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border border-primary/30 bg-primary/10">
            <ShieldCheck className="h-6 w-6 text-primary" aria-hidden="true" />
          </div>
          <div>
            <p className="text-sm font-medium text-muted-foreground">
              NOUS command line
            </p>
            <h1 className="mt-0.5 text-2xl font-semibold tracking-tight text-foreground">
              Connect the CLI to your account
            </h1>
          </div>
        </div>

        <p
          className="mb-5 max-w-lg text-[0.95rem] leading-7 text-muted-foreground"
          style={{ fontFamily: 'var(--nous-font-body)' }}
        >
          Approving this request lets the NOUS command line sign in as you and
          act on your current organization for up to 30 days. Type the code
          shown in your terminal to continue.
        </p>

        <div
          className="mb-6 flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/10 p-4"
          id="cli-auth-warning"
        >
          <AlertTriangle
            className="mt-0.5 h-5 w-5 shrink-0 text-destructive"
            aria-hidden="true"
          />
          <p className="text-sm leading-6 text-foreground">
            Only approve if you started this from your own terminal just now. If
            someone sent you this link or told you a code, cancel.
          </p>
        </div>

        <dl className="mb-6 space-y-4 rounded-xl border border-border bg-background/60 p-5">
          <div>
            <dt className="text-sm font-medium text-muted-foreground">
              Requested from (IP address)
            </dt>
            <dd className="mt-1.5 break-all font-mono text-sm text-foreground">
              {details?.requester_ip ??
                (details ? 'Unknown' : detailsError ? '—' : 'Loading…')}
            </dd>
          </div>
          <div>
            <dt className="text-sm font-medium text-muted-foreground">
              Device (as reported by the requester)
            </dt>
            <dd className="mt-1.5 break-all font-mono text-sm text-foreground">
              {details?.requester_user_agent ??
                (details ? 'Unknown' : detailsError ? '—' : 'Loading…')}
            </dd>
          </div>
          <div>
            <dt className="text-sm font-medium text-muted-foreground">
              Started
            </dt>
            <dd className="mt-1.5 text-sm text-foreground">
              {details
                ? formatStartedAt(details.started_at)
                : detailsError
                  ? '—'
                  : 'Loading…'}
            </dd>
          </div>
        </dl>
        <p className="-mt-4 mb-6 text-xs leading-5 text-muted-foreground">
          These details come from the device that started the request and may be
          inaccurate.
        </p>

        <label
          htmlFor="cli-auth-code"
          className="mb-2 block text-sm font-medium text-muted-foreground"
        >
          Code from your terminal
        </label>
        <input
          id="cli-auth-code"
          type="text"
          inputMode="text"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          placeholder="XXXX-XXXX"
          aria-describedby="cli-auth-warning"
          value={formatCode(codeChars)}
          onChange={(event) => setCodeChars(normalizeCode(event.target.value))}
          disabled={isConnected}
          className="mb-6 w-full rounded-lg border border-border bg-background px-3 py-2.5 font-mono text-lg tracking-[0.3em] text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        />

        {details && !isPending && !isConnected ? (
          <p className="mb-6 text-sm text-muted-foreground">
            This sign-in request is no longer waiting for approval (
            {sessionUnavailable ? 'unavailable' : details.status}). Run the
            login command in your terminal again.
          </p>
        ) : null}

        {isConnected ? (
          <div
            role="status"
            className="mb-6 flex items-start gap-3 rounded-xl border border-primary/30 bg-primary/10 p-5"
          >
            <CheckCircle2
              className="mt-0.5 h-5 w-5 shrink-0 text-primary"
              aria-hidden="true"
            />
            <div>
              <p className="text-sm font-semibold text-foreground">
                CLI connected. You can return to your terminal.
              </p>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                The pending NOUS terminal session will finish signing in
                automatically.
              </p>
            </div>
          </div>
        ) : null}

        {error || detailsError ? (
          <div
            role="alert"
            className="mb-6 rounded-xl border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive"
          >
            {error || detailsError}
          </div>
        ) : null}

        <div className="flex flex-col gap-3 sm:flex-row">
          <button
            type="button"
            onClick={handleApprove}
            disabled={!canApprove}
            className={cn(
              'inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-xl',
              'bg-primary px-5 text-sm font-semibold text-primary-foreground',
              'transition-colors hover:bg-primary/90',
              'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card',
              'disabled:cursor-not-allowed disabled:opacity-60'
            )}
          >
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            {isSubmitting ? 'Approving…' : 'Approve sign-in'}
          </button>
          <Link
            href="/login"
            className={cn(
              'inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-xl border border-border',
              'bg-transparent px-5 text-sm font-semibold text-muted-foreground',
              'transition-colors hover:border-foreground/30 hover:text-foreground',
              'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card'
            )}
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            Back to sign-in
          </Link>
        </div>
      </section>
    </main>
  );
}
