import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';

import ForgotPasswordPage from '../../../../app/(auth)/forgot-password/page';

const mockReset = vi.fn();

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({ auth: { resetPasswordForEmail: mockReset } }),
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

function expectLoginLayout(container: HTMLElement): void {
  // Same split as /login: scene panel, then the cream form panel as <main>.
  expect(
    container.querySelector('img[src="/landing/reading-room-login.svg"]')
  ).not.toBeNull();
  const main = screen.getByRole('main');
  expect(main.className).toContain('bg-(--nous-aurum)');
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  expect(container.innerHTML).not.toMatch(
    /bg-background|bg-card|text-foreground/
  );
  return main;
}

describe('ForgotPasswordPage', () => {
  beforeEach(() => {
    mockReset.mockReset();
  });

  it('renders the request form in the login split layout with its ids and hints intact', async () => {
    const { container } = render(<ForgotPasswordPage />);
    const main = expectLoginLayout(container);
    expect(
      within(main).getByRole('heading', {
        level: 2,
        name: 'Reset your password',
      })
    ).toBeInTheDocument();
    const email = await screen.findByLabelText('Email address');
    expect(email).toHaveAttribute('id', 'email');
    expect(email).toHaveAttribute('aria-describedby', 'email-hint');
    expect(container.querySelector('#email-hint')).toHaveTextContent(
      'We will only use this to send your reset link.'
    );
    expect(email.className).toContain('focus-visible:ring-2');
    expect(
      screen.getByRole('link', { name: /back to sign in/i })
    ).toHaveAttribute('href', '/login');
  });

  it('sends the reset link and shows the inbox state in the same layout', async () => {
    mockReset.mockResolvedValue({ error: null });
    const { container } = render(<ForgotPasswordPage />);
    fireEvent.change(await screen.findByLabelText('Email address'), {
      target: { value: 'ada@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }));

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Check your inbox' })
    ).toBeInTheDocument();
    expect(mockReset).toHaveBeenCalledWith('ada@example.com', {
      redirectTo: `${window.location.origin}/reset-password`,
    });
    expect(screen.getByText('ada@example.com')).toBeInTheDocument();
    expectLoginLayout(container);
  });

  it('shows a reset error as an alert', async () => {
    mockReset.mockResolvedValue({ error: { message: 'Rate limited' } });
    render(<ForgotPasswordPage />);
    fireEvent.change(await screen.findByLabelText('Email address'), {
      target: { value: 'ada@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Rate limited');
  });
});
