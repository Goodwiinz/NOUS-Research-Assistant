import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';

import LoginPage from '../../../../app/(auth)/login/page';
import RegisterPage from '../../../../app/(auth)/register/page';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    login: vi.fn(),
    register: vi.fn(),
    isAuthenticated: false,
    isLoading: false,
    pendingEmailConfirmation: false,
    pendingConfirmationEmail: null,
    pendingSignupPossiblyExisting: false,
    clearPendingEmailConfirmation: vi.fn(),
  }),
}));

vi.mock('@/services/nousCliAuth', () => ({
  downloadStoredNousCliAuth: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('next/link', () => ({
  __esModule: true,
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

/** The full stop sits before the footnote mark, with nothing after the mark but a space. */
function expectClosedUpFootnote(container: HTMLElement): void {
  const sup = container.querySelector('p sup') as HTMLElement;
  expect(sup).not.toBeNull();
  expect(sup.textContent).toBe('1');
  const before = sup.previousSibling?.textContent ?? '';
  const after = sup.nextSibling?.textContent ?? '';
  expect(before).toMatch(/\.$/);
  expect(after).not.toMatch(/^\s*\./);
  expect(sup.className).not.toMatch(/(^|\s)ml-/);
}

describe('auth page landmarks', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation(() => ({
        matches: false,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    });
  });

  it('wraps the login form panel in <main>', async () => {
    const { container } = render(<LoginPage />);
    const main = await screen.findByRole('main');
    expect(
      within(main).getByRole('heading', { name: 'Sign in' })
    ).toBeInTheDocument();
    expect(main.querySelector('form')).not.toBeNull();
    expectClosedUpFootnote(container);
  });

  it('wraps the register form panel in <main>', async () => {
    const { container } = render(<RegisterPage />);
    const main = await screen.findByRole('main');
    expect(
      within(main).getByRole('heading', { name: 'Create your account' })
    ).toBeInTheDocument();
    expect(main.querySelector('form')).not.toBeNull();
    expectClosedUpFootnote(container);
  });
});
