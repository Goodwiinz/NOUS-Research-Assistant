'use client';

import AuthScenePanel from '@/components/auth/AuthScenePanel';
import { createClient } from '@/lib/supabase/client';
import { cn } from '@/lib/utils';
import { ArrowLeft, Mail } from 'lucide-react';
import Link from 'next/link';
import React, { useState } from 'react';

// Same reading-room scene and wash as /login: this page is a step of the
// sign-in flow, so it keeps the sign-in look.
const SCENE = {
  src: '/landing/reading-room-login.svg',
  headline: 'Pick up your reading where you left it.',
  notes: [
    'If no account exists for that address, no email is sent, and this page says the same either way.',
  ],
} as const;

const SceneBody = (
  <>
    Resetting your password changes how you sign in, not what you have read.
    Your documents and notes stay where they are.
    <sup className="align-super text-[0.6em] font-semibold text-(--nous-helios)">
      1
    </sup>
  </>
);

// Shared with /login (inputClass, labelClass, submit button, footer link).
const inputClass =
  'h-12 w-full rounded-(--nous-radius-md) border border-(--nous-enceladus) bg-(--nous-bg-2) pl-11 pr-4 text-sm text-(--nous-fg-1) placeholder:text-(--nous-fg-3) outline-hidden transition-colors focus-visible:border-(--nous-sol) focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40';
const labelClass = 'block text-sm font-medium text-(--nous-titan)';
const backLinkClass =
  'inline-flex items-center gap-2 rounded-sm text-sm font-medium text-(--nous-sol-safe) underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40';
const headingStyle = { fontFamily: 'var(--nous-font-body)' } as const;

export default function ForgotPasswordPage(): React.JSX.Element {
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const handleSubmit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    setError('');
    setIsSubmitting(true);

    try {
      const supabase = createClient();
      const { error: resetError } = await supabase.auth.resetPasswordForEmail(
        email,
        { redirectTo: `${window.location.origin}/reset-password` }
      );

      if (resetError) {
        throw new Error(resetError.message);
      }

      setSent(true);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Failed to send reset link'
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      <AuthScenePanel
        src={SCENE.src}
        headline={SCENE.headline}
        body={SceneBody}
        notes={[...SCENE.notes]}
        wash="heavy"
        proseTone="ivory"
      />

      <main className="flex items-center justify-center bg-(--nous-aurum) px-5 py-8 lg:px-8 lg:py-16">
        <div className="w-full max-w-[440px]">
          {sent ? (
            <div className="flex flex-col gap-5">
              <div>
                <h2
                  className="mb-1.5 text-[32px] font-normal tracking-[-0.02em] text-(--nous-erebus)"
                  style={headingStyle}
                >
                  Check your inbox
                </h2>
                <p
                  className="text-[15px] leading-relaxed text-(--nous-titan)"
                  style={headingStyle}
                >
                  If an account exists for{' '}
                  <span className="font-medium text-(--nous-erebus)">
                    {email}
                  </span>
                  , a password reset link is on its way. Follow it to choose a
                  new password.
                </p>
              </div>
              <p className="mt-2 border-t border-(--nous-enceladus) pt-5">
                <Link href="/login" className={backLinkClass}>
                  <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                  Back to sign in
                </Link>
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-5">
              <div>
                <h2
                  className="mb-1.5 text-[32px] font-normal tracking-[-0.02em] text-(--nous-erebus)"
                  style={headingStyle}
                >
                  Reset your password
                </h2>
                <p className="text-sm leading-relaxed text-(--nous-titan)">
                  Enter the email address linked to your account and we will
                  send you a link to set a new password.
                </p>
              </div>

              <form onSubmit={handleSubmit} className="flex flex-col gap-5">
                {error && (
                  <div
                    role="alert"
                    className="rounded-(--nous-radius-md) border border-(--nous-mars)/40 bg-(--nous-mars)/10 p-3"
                  >
                    <p className="text-sm text-(--nous-mars)">{error}</p>
                  </div>
                )}

                <div className="space-y-1.5">
                  <label htmlFor="email" className={labelClass}>
                    Email address
                  </label>
                  <div className="relative">
                    <div
                      className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5"
                      aria-hidden="true"
                    >
                      <Mail className="h-4 w-4 text-(--nous-fg-3)" />
                    </div>
                    <input
                      id="email"
                      name="email"
                      type="email"
                      required
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      aria-describedby="email-hint"
                      className={inputClass}
                      placeholder="you@example.com"
                      autoComplete="email"
                    />
                  </div>
                  <p id="email-hint" className="text-xs text-(--nous-fg-3)">
                    We will only use this to send your reset link.
                  </p>
                </div>

                <button
                  type="submit"
                  disabled={isSubmitting}
                  className={cn(
                    'flex h-12 w-full items-center justify-center rounded-(--nous-radius-md) bg-(--nous-erebus) text-sm font-semibold text-(--nous-selene) transition-colors hover:bg-(--nous-titan)',
                    'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-aurum)',
                    'disabled:cursor-not-allowed disabled:opacity-70'
                  )}
                >
                  {isSubmitting ? 'Sending...' : 'Send reset link'}
                </button>
              </form>

              <p className="mt-2 border-t border-(--nous-enceladus) pt-5 text-center">
                <Link href="/login" className={backLinkClass}>
                  <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                  Back to sign in
                </Link>
              </p>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
