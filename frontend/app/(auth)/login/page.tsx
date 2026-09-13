'use client';

import AuthScenePanel from '@/components/auth/AuthScenePanel';
import { useAuth } from '@/hooks/useAuth';
import { cn } from '@/lib/utils';
import { downloadStoredNousCliAuth } from '@/services/nousCliAuth';
import { getSafeAuthRedirect } from '@/utils/authRedirect';
import { Lock, Mail } from 'lucide-react';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import React, { useEffect, useState } from 'react';

const ArrowRight = dynamic(
  () => import('lucide-react').then((mod) => mod.ArrowRight),
  { ssr: false }
);
const Eye = dynamic(() => import('lucide-react').then((mod) => mod.Eye), {
  ssr: false,
});
const EyeOff = dynamic(() => import('lucide-react').then((mod) => mod.EyeOff), {
  ssr: false,
});

interface LoginFormData {
  email: string;
  password: string;
  downloadCliAuth: boolean;
}

const SCENE = {
  src: '/landing/reading-room-login.svg',
  headline: 'Turn your sources into answers you can trust.',
  notes: [
    'Every answer carries inline citations that resolve to the retrieved passage, page and document.',
    'Retrieval is hybrid: vector, BM25 and knowledge-graph traversal.',
  ],
} as const;

const SceneBody = (
  <>
    NOUS reads your documents the way a careful researcher would, and shows its
    work
    <sup className="ml-0.5 align-super text-[0.6em] font-semibold text-(--nous-helios)">
      1
    </sup>
    . Sign in to pick up where you left off.
  </>
);

function resolvePostLoginPath(rawNextPath: string | null): string {
  if (!rawNextPath || !rawNextPath.startsWith('/')) {
    return '/dashboard';
  }

  // SECURITY: a hand-rolled "starts with / but not //" check is not enough —
  // `/\evil.com` passes it, and WHATWG URL parsing (what the browser applies
  // when the router resolves the href) treats `/\` exactly like `//`, so the
  // push lands off-origin. Delegate to the single shared sanitizer that the
  // server-side auth callback already uses instead of writing a second one.
  const origin =
    typeof window === 'undefined' ? 'http://localhost' : window.location.origin;

  return getSafeAuthRedirect(rawNextPath, origin, '/dashboard');
}

function describeAuthCallbackError(code: string | null): string {
  // SECURITY (audit #23): never reflect the raw error_description from the
  // redirect URL — it's attacker-controlled and would render a fabricated
  // message verbatim. Map the known error CODE to a hardcoded message only.
  if (!code) return '';
  switch (code) {
    case 'auth_callback_failed':
      return 'Authentication callback failed. Please try signing in again.';
    case 'access_denied':
      return 'The confirmation link was rejected. It may have expired or already been used.';
    case 'otp_expired':
    case 'expired_link':
      return 'This confirmation link has expired. Request a new one from registration.';
    case 'exchange_failed':
      return 'We could not establish a session from the confirmation link.';
    case 'missing_code':
      return 'The confirmation link is missing its verification code.';
    default:
      return 'Authentication failed.';
  }
}

function LoginPageContent(): React.JSX.Element | null {
  const { login, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = resolvePostLoginPath(searchParams.get('next'));
  const callbackError = describeAuthCallbackError(searchParams.get('error'));
  const chatRecoveryMessage =
    searchParams.get('reauth') === 'chat'
      ? searchParams.get('draft') === 'saved'
        ? "Please sign in again to continue. Sign in to the same account to return to this conversation. Your message text is saved in this tab and won't be sent until you press Send."
        : 'Please sign in again to continue.'
      : null;
  const [formData, setFormData] = useState<LoginFormData>({
    email: '',
    password: '',
    downloadCliAuth: false,
  });
  const [error, setError] = useState<string>(callbackError);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    // Hydration guard: flip once after mount so the SSR skeleton is replaced.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMounted(true);
  }, []);

  useEffect(() => {
    if (isAuthenticated && !isLoading) {
      router.push(nextPath);
    }
  }, [isAuthenticated, isLoading, nextPath, router]);

  const handleSubmit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    setError('');
    setIsSubmitting(true);

    try {
      await login(formData.email, formData.password);
      if (formData.downloadCliAuth) {
        try {
          downloadStoredNousCliAuth();
        } catch (downloadError) {
          console.error('Failed to export NOUS CLI auth:', downloadError);
        }
      }
      router.push(nextPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Authentication failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    setFormData((prev) => ({
      ...prev,
      [e.target.name]:
        e.target.type === 'checkbox' ? e.target.checked : e.target.value,
    }));
  };

  const inputClass =
    'h-12 w-full rounded-(--nous-radius-md) border border-(--nous-enceladus) bg-(--nous-bg-2) text-sm text-(--nous-fg-1) placeholder:text-(--nous-fg-3) outline-hidden transition-colors focus-visible:border-(--nous-sol) focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40';
  const labelClass = 'block text-sm font-medium text-(--nous-titan)';

  const Scene = (
    <AuthScenePanel
      src={SCENE.src}
      headline={SCENE.headline}
      body={SceneBody}
      notes={[...SCENE.notes]}
      wash="heavy"
      proseTone="ivory"
    />
  );

  // SSR-friendly skeleton to keep LCP stable while hydrating.
  if (!mounted) {
    return (
      <div className="grid min-h-screen lg:grid-cols-2">
        {Scene}
        <div className="flex items-center justify-center bg-(--nous-aurum) px-5 py-8 lg:px-8 lg:py-16">
          <div className="w-full max-w-[440px] space-y-5">
            <h2
              className="text-[32px] font-normal tracking-[-0.02em] text-(--nous-erebus)"
              style={{ fontFamily: 'var(--nous-font-body)' }}
            >
              Sign in
            </h2>
            <div className="h-12 rounded-(--nous-radius-md) bg-(--nous-bg-2)" />
            <div className="h-12 rounded-(--nous-radius-md) bg-(--nous-bg-2)" />
            <div className="h-12 rounded-(--nous-radius-md) bg-(--nous-erebus)/10" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      {Scene}

      {/* Right panel — sign-in form */}
      <div className="flex items-center justify-center bg-(--nous-aurum) px-5 py-8 lg:px-8 lg:py-16">
        <div className="w-full max-w-[440px]">
          <div className="flex flex-col gap-5">
            <div>
              <h2
                className="mb-1.5 text-[32px] font-normal tracking-[-0.02em] text-(--nous-erebus)"
                style={{ fontFamily: 'var(--nous-font-body)' }}
              >
                Sign in
              </h2>
              <p className="text-sm text-(--nous-fg-3)">
                Welcome back to NOUS.
              </p>
            </div>

            <form onSubmit={handleSubmit} className="flex flex-col gap-5">
              {chatRecoveryMessage ? (
                <div className="rounded-(--nous-radius-md) border border-(--nous-sol)/30 bg-(--nous-sol)/10 p-3">
                  <p className="text-sm text-(--nous-titan)">
                    {chatRecoveryMessage}
                  </p>
                </div>
              ) : null}

              {error && (
                <div
                  role="alert"
                  className="rounded-(--nous-radius-md) border border-(--nous-mars)/40 bg-(--nous-mars)/10 p-3"
                >
                  <p className="text-sm text-(--nous-mars)">{error}</p>
                </div>
              )}

              {/* Email */}
              <div className="space-y-1.5">
                <label htmlFor="email" className={labelClass}>
                  Email
                </label>
                <div className="relative">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <Mail
                      aria-hidden="true"
                      className="h-4 w-4 text-(--nous-fg-3)"
                    />
                  </div>
                  <input
                    id="email"
                    data-testid="email-input"
                    name="email"
                    type="email"
                    required
                    value={formData.email}
                    onChange={handleChange}
                    className={cn(inputClass, 'pl-11 pr-4')}
                    placeholder="you@example.com"
                    autoComplete="email"
                  />
                </div>
              </div>

              {/* Password */}
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <label htmlFor="password" className={labelClass}>
                    Password
                  </label>
                  <Link
                    href="/forgot-password"
                    className="rounded-sm text-sm text-(--nous-sol-safe) underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
                  >
                    Forgot password?
                  </Link>
                </div>
                <div className="relative">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <Lock
                      aria-hidden="true"
                      className="h-4 w-4 text-(--nous-fg-3)"
                    />
                  </div>
                  <input
                    id="password"
                    data-testid="password-input"
                    name="password"
                    type={showPassword ? 'text' : 'password'}
                    required
                    value={formData.password}
                    onChange={handleChange}
                    className={cn(inputClass, 'pl-11 pr-12')}
                    placeholder="Your password"
                    autoComplete="current-password"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-sm text-(--nous-fg-3) transition-colors hover:text-(--nous-sol-safe) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
                    aria-label={
                      showPassword ? 'Hide password' : 'Show password'
                    }
                  >
                    {showPassword ? (
                      <EyeOff aria-hidden="true" className="h-4 w-4" />
                    ) : (
                      <Eye aria-hidden="true" className="h-4 w-4" />
                    )}
                  </button>
                </div>
              </div>

              {/* CLI auth export */}
              <label
                htmlFor="downloadCliAuth"
                className="flex min-h-11 cursor-pointer items-center gap-2.5 text-sm text-(--nous-titan)"
              >
                <input
                  id="downloadCliAuth"
                  name="downloadCliAuth"
                  type="checkbox"
                  checked={formData.downloadCliAuth}
                  onChange={handleChange}
                  className="h-[18px] w-[18px] rounded-(--nous-radius-sm) border-(--nous-border-2) bg-(--nous-bg-2) accent-(--nous-sol) focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
                />
                Also download a NOUS CLI credential
              </label>

              {/* Submit */}
              <button
                type="submit"
                data-testid="login-button"
                disabled={isSubmitting}
                className="group flex h-12 w-full items-center justify-center gap-2 rounded-(--nous-radius-md) bg-(--nous-erebus) text-sm font-semibold text-(--nous-selene) transition-colors hover:bg-(--nous-titan) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-aurum) disabled:cursor-not-allowed disabled:opacity-70"
              >
                {isSubmitting ? (
                  <span>Signing in…</span>
                ) : (
                  <>
                    <span>Sign in</span>
                    <ArrowRight
                      aria-hidden="true"
                      className="h-4 w-4 transition-transform group-hover:translate-x-0.5"
                    />
                  </>
                )}
              </button>
            </form>

            <p className="mt-2 border-t border-(--nous-enceladus) pt-5 text-center text-sm text-(--nous-titan)">
              New to NOUS?{' '}
              <Link
                href="/register"
                className="rounded-sm font-medium text-(--nous-sol-safe) underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
              >
                Create an account
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function LoginPage(): React.JSX.Element {
  return (
    <React.Suspense fallback={null}>
      <LoginPageContent />
    </React.Suspense>
  );
}
