import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthProvider } from '@/hooks/useAuth';
import { useAuthStore } from '@/stores/authStore';
import LoginPage from '../../../../app/(auth)/login/page';
import ForgotPasswordPage from '../../../../app/(auth)/forgot-password/page';

const auth = vi.hoisted(() => ({
  getUser: vi.fn(),
  signOut: vi.fn(),
  resetPasswordForEmail: vi.fn(),
  listener: undefined as
    ((event: string, session: unknown) => void) | undefined,
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      ...auth,
      onAuthStateChange: (listener: typeof auth.listener) => {
        auth.listener = listener;
        return { data: { subscription: { unsubscribe: vi.fn() } } };
      },
    },
  }),
}));
vi.mock('@/lib/supabase/clearAuthCookies', () => ({
  clearSupabaseAuthCookies: vi.fn(),
}));
vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), clearAuth: vi.fn() },
}));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock('next/link', () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: ReactNode;
    href: string;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(async () => {
  auth.signOut.mockResolvedValue({ error: null });
  await useAuthStore.getState().signOut();
});

it.each(['no user', 'missing session', 'verification exception', 'signed out'])(
  'preserves a login draft when anonymous bootstrap reports %s',
  async (outcome) => {
    const verification = deferred<unknown>();
    auth.getUser.mockReturnValueOnce(verification.promise);
    render(
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    );
    await waitFor(() => expect(auth.getUser).toHaveBeenCalledOnce());
    const initialized = useAuthStore.getState().fetchProfile();
    fireEvent.change(await screen.findByTestId('email-input'), {
      target: { value: 'new-visitor@example.invalid' },
    });
    fireEvent.change(screen.getByTestId('password-input'), {
      target: { value: 'synthetic-password' },
    });

    await act(async () => {
      if (outcome === 'signed out') auth.listener!('SIGNED_OUT', null);
      if (outcome === 'verification exception') {
        verification.reject(new Error('Verification unavailable'));
      } else {
        verification.resolve({
          data: { user: null },
          error:
            outcome === 'missing session'
              ? new Error('Auth session missing')
              : null,
        });
      }
      await initialized;
    });

    expect(useAuthStore.getState().isLoading).toBe(false);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(screen.getByTestId('email-input')).toHaveValue(
      'new-visitor@example.invalid'
    );
    expect(screen.getByTestId('password-input')).toHaveValue(
      'synthetic-password'
    );
  }
);

it('preserves a password-reset submission while anonymous bootstrap settles', async () => {
  const verification = deferred<unknown>();
  const reset = deferred<unknown>();
  auth.getUser.mockReturnValueOnce(verification.promise);
  auth.resetPasswordForEmail.mockReturnValueOnce(reset.promise);
  render(
    <AuthProvider>
      <ForgotPasswordPage />
    </AuthProvider>
  );
  await waitFor(() => expect(auth.getUser).toHaveBeenCalledOnce());
  const initialized = useAuthStore.getState().fetchProfile();
  fireEvent.change(await screen.findByLabelText('Email address'), {
    target: { value: 'new-visitor@example.invalid' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }));
  expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled();

  await act(async () => {
    verification.resolve({ data: { user: null }, error: null });
    await initialized;
    reset.resolve({ error: null });
    await reset.promise;
  });

  expect(
    screen.getByRole('heading', { name: 'Check your inbox' })
  ).toBeInTheDocument();
  expect(screen.getByText('new-visitor@example.invalid')).toBeInTheDocument();
});
