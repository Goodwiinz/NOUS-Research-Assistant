'use client';

import AuthScenePanel from '@/components/auth/AuthScenePanel';
import PendingEmailConfirmation from '@/components/auth/PendingEmailConfirmation';
import { useAuth } from '@/hooks/useAuth';
import { cn } from '@/lib/utils';
import { RegisterRequest } from '@/types';
import {
  ArrowRight,
  Building2,
  Eye,
  EyeOff,
  Lock,
  Mail,
  User,
} from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import React, { useEffect, useState } from 'react';

interface RegisterFormData {
  email: string;
  password: string;
  confirmPassword: string;
  first_name: string;
  last_name: string;
  organization_name: string;
}

const SCENE = {
  src: '/landing/reading-room-register.svg',
  headline: 'Make sense of everything you read.',
  notes: [
    'Inference can run in the browser with WebLLM. Your corpus stays on your infrastructure.',
    'Role-based access is enforced inside the retrieval pipeline.',
  ],
} as const;

const SceneBody = (
  <>
    Create an account to search, connect and synthesize your documents in one
    place
    <sup className="ml-0.5 align-super text-[0.6em] font-semibold text-(--nous-helios)">
      1
    </sup>
    .
  </>
);

export default function RegisterPage() {
  const {
    register,
    isAuthenticated,
    isLoading,
    pendingEmailConfirmation,
    pendingConfirmationEmail,
    pendingSignupPossiblyExisting,
    clearPendingEmailConfirmation,
  } = useAuth();
  const router = useRouter();
  const [formData, setFormData] = useState<RegisterFormData>({
    email: '',
    password: '',
    confirmPassword: '',
    first_name: '',
    last_name: '',
    organization_name: '',
  });
  const [error, setError] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    // Hydration guard: flip once after mount so the SSR skeleton is replaced.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMounted(true);
  }, []);

  // Redirect if already authenticated. '/' is the public marketing landing and
  // does not bounce authenticated visitors anywhere, so sending them there
  // would drop them back on the anonymous page; match the login page's
  // destination instead.
  useEffect(() => {
    if (isAuthenticated && !isLoading) {
      router.push('/dashboard');
    }
  }, [isAuthenticated, isLoading, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    // Validate passwords match
    if (formData.password !== formData.confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    // Validate password strength
    if (formData.password.length < 8) {
      setError('Password must be at least 8 characters');
      return;
    }

    setIsSubmitting(true);

    try {
      const registerData: RegisterRequest = {
        email: formData.email,
        password: formData.password,
        first_name: formData.first_name,
        last_name: formData.last_name,
        organization_name: formData.organization_name || undefined,
      };

      const result = await register(registerData);
      if (!result.requiresEmailConfirmation) {
        // Signup issued a session — land on the app, not the marketing page.
        router.push('/dashboard');
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Could not create your account'
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData((prev) => ({
      ...prev,
      [e.target.name]: e.target.value,
    }));
  };

  const passwordStrength = () => {
    const password = formData.password;
    if (!password) return { level: 0, text: '', color: '' };
    if (password.length < 6)
      return { level: 1, text: 'Weak', color: 'bg-(--nous-mars)' };
    if (password.length < 8)
      return { level: 2, text: 'Fair', color: 'bg-(--nous-corona)' };
    if (password.length < 12)
      return { level: 3, text: 'Good', color: 'bg-(--nous-sol)' };
    return { level: 4, text: 'Strong', color: 'bg-(--nous-terra)' };
  };

  const strength = passwordStrength();

  const handleResetConfirmation = () => {
    clearPendingEmailConfirmation();
    setFormData({
      email: '',
      password: '',
      confirmPassword: '',
      first_name: '',
      last_name: '',
      organization_name: '',
    });
  };

  if (!mounted) return null;

  // Email confirmation screen
  if (pendingEmailConfirmation) {
    return (
      <PendingEmailConfirmation
        // The store is the source of truth: `formData.email` is local state
        // that empties on remount, which used to blank the address out.
        email={pendingConfirmationEmail ?? formData.email}
        possiblyExisting={pendingSignupPossiblyExisting}
        onReset={handleResetConfirmation}
      />
    );
  }

  const inputClasses =
    'h-12 w-full rounded-(--nous-radius-md) border border-(--nous-enceladus) bg-(--nous-bg-2) pl-11 pr-4 text-sm text-(--nous-fg-1) placeholder:text-(--nous-fg-3) outline-hidden transition-colors focus-visible:border-(--nous-sol) focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40';
  const labelClasses = 'block text-sm font-medium text-(--nous-titan)';
  const toggleClasses =
    'absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-sm text-(--nous-fg-3) transition-colors hover:text-(--nous-sol-safe) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40';

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      <AuthScenePanel
        src={SCENE.src}
        headline={SCENE.headline}
        body={SceneBody}
        notes={[...SCENE.notes]}
        wash="light"
        proseTone="parchment"
      />

      {/* Right panel — form */}
      <div className="flex items-center justify-center bg-(--nous-aurum) px-5 py-8 lg:px-8 lg:py-16">
        <div className="w-full max-w-[440px]">
          <div className="flex flex-col gap-5">
            <div>
              <h2
                className="mb-1.5 text-[32px] font-normal tracking-[-0.02em] text-(--nous-erebus)"
                style={{ fontFamily: 'var(--nous-font-body)' }}
              >
                Create your account
              </h2>
              <p className="text-sm text-(--nous-fg-3)">
                Set up access for you and your team.
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

              {/* Name fields */}
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <label htmlFor="first_name" className={labelClasses}>
                    First name
                  </label>
                  <div className="relative">
                    <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                      <User
                        className="h-4 w-4 text-(--nous-fg-3)"
                        aria-hidden="true"
                      />
                    </div>
                    <input
                      id="first_name"
                      name="first_name"
                      type="text"
                      required
                      value={formData.first_name}
                      onChange={handleChange}
                      className={inputClasses}
                      placeholder="Ada"
                    />
                  </div>
                </div>

                <div className="space-y-1.5">
                  <label htmlFor="last_name" className={labelClasses}>
                    Last name
                  </label>
                  <div className="relative">
                    <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                      <User
                        className="h-4 w-4 text-(--nous-fg-3)"
                        aria-hidden="true"
                      />
                    </div>
                    <input
                      id="last_name"
                      name="last_name"
                      type="text"
                      required
                      value={formData.last_name}
                      onChange={handleChange}
                      className={inputClasses}
                      placeholder="Lovelace"
                    />
                  </div>
                </div>
              </div>

              {/* Email */}
              <div className="space-y-1.5">
                <label htmlFor="email" className={labelClasses}>
                  Email
                </label>
                <div className="relative">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <Mail
                      className="h-4 w-4 text-(--nous-fg-3)"
                      aria-hidden="true"
                    />
                  </div>
                  <input
                    id="email"
                    name="email"
                    type="email"
                    required
                    value={formData.email}
                    onChange={handleChange}
                    className={inputClasses}
                    placeholder="you@company.com"
                  />
                </div>
              </div>

              {/* Organization */}
              <div className="space-y-1.5">
                <label htmlFor="organization_name" className={labelClasses}>
                  Organization{' '}
                  <span className="font-normal text-(--nous-fg-3)">
                    (optional)
                  </span>
                </label>
                <div className="relative">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <Building2
                      className="h-4 w-4 text-(--nous-fg-3)"
                      aria-hidden="true"
                    />
                  </div>
                  <input
                    id="organization_name"
                    name="organization_name"
                    type="text"
                    value={formData.organization_name}
                    onChange={handleChange}
                    className={inputClasses}
                    placeholder="Acme Inc."
                  />
                </div>
              </div>

              {/* Password */}
              <div className="space-y-1.5">
                <label htmlFor="password" className={labelClasses}>
                  Password
                </label>
                <div className="relative">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <Lock
                      className="h-4 w-4 text-(--nous-fg-3)"
                      aria-hidden="true"
                    />
                  </div>
                  <input
                    id="password"
                    name="password"
                    type={showPassword ? 'text' : 'password'}
                    required
                    value={formData.password}
                    onChange={handleChange}
                    className={cn(inputClasses, 'pr-12')}
                    placeholder="At least 8 characters"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    aria-label={
                      showPassword ? 'Hide password' : 'Show password'
                    }
                    aria-pressed={showPassword}
                    className={toggleClasses}
                  >
                    {showPassword ? (
                      <EyeOff className="h-4 w-4" aria-hidden="true" />
                    ) : (
                      <Eye className="h-4 w-4" aria-hidden="true" />
                    )}
                  </button>
                </div>
                {/* Strength */}
                {formData.password && (
                  <div className="mt-2 flex items-center gap-2">
                    <div className="flex flex-1 gap-1" aria-hidden="true">
                      {[1, 2, 3, 4].map((level) => (
                        <div
                          key={level}
                          className={cn(
                            'h-1 flex-1 rounded-full transition-colors duration-300',
                            level <= strength.level
                              ? strength.color
                              : 'bg-(--nous-enceladus)'
                          )}
                        />
                      ))}
                    </div>
                    <span className="text-xs text-(--nous-fg-3)">
                      {strength.text}
                    </span>
                  </div>
                )}
              </div>

              {/* Confirm password */}
              <div className="space-y-1.5">
                <label htmlFor="confirmPassword" className={labelClasses}>
                  Confirm password
                </label>
                <div className="relative">
                  <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3.5">
                    <Lock
                      className="h-4 w-4 text-(--nous-fg-3)"
                      aria-hidden="true"
                    />
                  </div>
                  <input
                    id="confirmPassword"
                    name="confirmPassword"
                    type={showConfirmPassword ? 'text' : 'password'}
                    required
                    value={formData.confirmPassword}
                    onChange={handleChange}
                    className={cn(inputClasses, 'pr-12')}
                    placeholder="Re-enter your password"
                  />
                  <button
                    type="button"
                    onClick={() =>
                      showConfirmPassword
                        ? setShowConfirmPassword(false)
                        : setShowConfirmPassword(true)
                    }
                    aria-label={
                      showConfirmPassword
                        ? 'Hide confirmation password'
                        : 'Show confirmation password'
                    }
                    aria-pressed={showConfirmPassword}
                    className={toggleClasses}
                  >
                    {showConfirmPassword ? (
                      <EyeOff className="h-4 w-4" aria-hidden="true" />
                    ) : (
                      <Eye className="h-4 w-4" aria-hidden="true" />
                    )}
                  </button>
                </div>
              </div>

              {/* Submit */}
              <button
                type="submit"
                disabled={isSubmitting}
                className="group flex h-12 w-full items-center justify-center gap-2 rounded-(--nous-radius-md) bg-(--nous-erebus) text-sm font-semibold text-(--nous-selene) transition-colors hover:bg-(--nous-titan) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-aurum) disabled:cursor-not-allowed disabled:opacity-60"
              >
                {isSubmitting ? (
                  <span>Creating account…</span>
                ) : (
                  <>
                    <span>Create account</span>
                    <ArrowRight
                      className="h-4 w-4 transition-transform group-hover:translate-x-0.5"
                      aria-hidden="true"
                    />
                  </>
                )}
              </button>
            </form>

            <p className="mt-2 border-t border-(--nous-enceladus) pt-5 text-center text-sm text-(--nous-titan)">
              Already have an account?{' '}
              <Link
                href="/login"
                className="rounded-sm font-medium text-(--nous-sol-safe) underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
              >
                Sign in
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
