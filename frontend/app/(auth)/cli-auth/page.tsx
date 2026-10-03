'use client';

import { useAuth } from '@/hooks/useAuth';
import { cn } from '@/lib/utils';
import { api } from '@/services/api-client';
import { ArrowLeft, CheckCircle2, ShieldCheck } from 'lucide-react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import React, { useEffect, useState } from 'react';

function CliAuthPageContent(): React.JSX.Element {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const sessionId = searchParams.get('session_id') ?? '';
  // Older backends put the code in the query string; current ones use the fragment.
  const legacyQueryCode = searchParams.get('code') ?? '';
  const [verificationCode, setVerificationCode] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState('');

  // Keep the code out of every URL a server sees: it is carried across the
  // login redirect in sessionStorage and stripped from the address bar.
  useEffect(() => {
    const storageKey = `nous:cli-auth-code:${sessionId}`;
    const urlCode =
      new URLSearchParams(window.location.hash.slice(1)).get('code') ||
      legacyQueryCode;
    let code = urlCode;
    try {
      if (urlCode) {
        sessionStorage.setItem(storageKey, urlCode);
      } else {
        code = sessionStorage.getItem(storageKey) ?? '';
      }
    } catch {
      // Storage unavailable: the code only survives this page view.
    }
    // The fragment and sessionStorage only exist in the browser, after mount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setVerificationCode((previous) => code || previous);
    if (urlCode) {
      window.history.replaceState(
        null,
        '',
        `/cli-auth?session_id=${encodeURIComponent(sessionId)}`
      );
    }
  }, [sessionId, legacyQueryCode]);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      const next = `/cli-auth?session_id=${encodeURIComponent(sessionId)}`;
      router.push(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [isAuthenticated, isLoading, router, sessionId]);

  const handleApprove = async (): Promise<void> => {
    if (!sessionId || !verificationCode || isSubmitting) {
      return;
    }

    setError('');
    setIsSubmitting(true);

    try {
      await api.post('/cli-auth/approve', {
        session_id: sessionId,
        verification_code: verificationCode,
      });
      setIsConnected(true);
    } catch (approveError) {
      setError(
        approveError instanceof Error
          ? approveError.message
          : 'Failed to approve NOUS CLI login.'
      );
    } finally {
      setIsSubmitting(false);
    }
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
          className="mb-7 max-w-lg text-[0.95rem] leading-7 text-muted-foreground"
          style={{ fontFamily: 'var(--nous-font-body)' }}
        >
          Approving this request lets the NOUS command line sign in as you and
          act on your current organization. Check the code below matches the one
          shown in your terminal.
        </p>

        <dl className="mb-7 space-y-4 rounded-xl border border-border bg-background/60 p-5">
          <div>
            <dt className="text-sm font-medium text-muted-foreground">
              Session ID
            </dt>
            <dd className="mt-1.5 break-all font-mono text-sm text-foreground">
              {sessionId || 'No session ID provided'}
            </dd>
          </div>
          <div>
            <dt className="text-sm font-medium text-muted-foreground">
              Verification code
            </dt>
            <dd className="mt-1.5 font-mono text-lg tracking-[0.3em] text-primary">
              {verificationCode || 'Missing'}
            </dd>
          </div>
        </dl>

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

        {error ? (
          <div
            role="alert"
            className="mb-6 rounded-xl border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive"
          >
            {error}
          </div>
        ) : null}

        <div className="flex flex-col gap-3 sm:flex-row">
          <button
            type="button"
            aria-label="Approve CLI login"
            onClick={handleApprove}
            disabled={
              !sessionId || !verificationCode || isSubmitting || isConnected
            }
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
            Cancel
          </Link>
        </div>
      </section>
    </main>
  );
}

export default function CliAuthPage(): React.JSX.Element {
  return (
    <React.Suspense fallback={null}>
      <CliAuthPageContent />
    </React.Suspense>
  );
}
