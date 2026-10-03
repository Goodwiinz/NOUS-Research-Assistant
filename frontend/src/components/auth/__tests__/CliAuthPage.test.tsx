import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { api } from '@/services/api-client';

import CliAuthPage from '../../../../app/(auth)/cli-auth/page';

const mockPush = vi.fn();

const mockedAuth = {
  isAuthenticated: true,
  isLoading: false,
};

let mockSearchParams = new URLSearchParams(
  'session_id=session-1&code=ABCD-1234'
);

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockedAuth,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: mockPush,
  }),
  useSearchParams: () => mockSearchParams,
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

describe('CliAuthPage', () => {
  beforeEach(() => {
    mockPush.mockReset();
    vi.restoreAllMocks();
    mockedAuth.isAuthenticated = true;
    mockedAuth.isLoading = false;
    mockSearchParams = new URLSearchParams(
      'session_id=session-1&code=ABCD-1234'
    );
    window.sessionStorage.clear();
    window.history.replaceState(null, '', '/');
  });

  it('shows approve action for authenticated browser session', async () => {
    render(<CliAuthPage />);

    expect(
      await screen.findByRole('button', { name: /approve/i })
    ).toBeInTheDocument();
  });

  it('approves the CLI login and shows the connected state', async () => {
    const postSpy = vi
      .spyOn(api, 'post')
      .mockResolvedValue({ status: 'approved' } as never);

    render(<CliAuthPage />);

    fireEvent.click(await screen.findByRole('button', { name: /approve/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('/cli-auth/approve', {
        session_id: 'session-1',
        verification_code: 'ABCD-1234',
      })
    );
    expect(
      await screen.findByText(
        /cli connected\. you can return to your terminal/i
      )
    ).toBeInTheDocument();
  });

  it('reads the code from the URL fragment and strips it from the address bar', async () => {
    mockSearchParams = new URLSearchParams('session_id=session-1');
    window.history.replaceState(
      null,
      '',
      '/cli-auth?session_id=session-1#code=WXYZ-9876'
    );
    const postSpy = vi
      .spyOn(api, 'post')
      .mockResolvedValue({ status: 'approved' } as never);

    render(<CliAuthPage />);

    fireEvent.click(await screen.findByRole('button', { name: /approve/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('/cli-auth/approve', {
        session_id: 'session-1',
        verification_code: 'WXYZ-9876',
      })
    );
    expect(window.location.href).not.toContain('WXYZ-9876');
  });

  it('keeps the code out of the login redirect URL', async () => {
    mockedAuth.isAuthenticated = false;
    mockSearchParams = new URLSearchParams('session_id=session-1');
    window.history.replaceState(
      null,
      '',
      '/cli-auth?session_id=session-1#code=WXYZ-9876'
    );

    render(<CliAuthPage />);

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith(
        `/login?next=${encodeURIComponent('/cli-auth?session_id=session-1')}`
      )
    );
    expect(mockPush.mock.calls.flat().join(' ')).not.toContain('WXYZ-9876');
    expect(window.sessionStorage.getItem('nous:cli-auth-code:session-1')).toBe(
      'WXYZ-9876'
    );
  });

  it('lets the user type the terminal code when the link carries none', async () => {
    mockSearchParams = new URLSearchParams('session_id=session-1');
    const postSpy = vi
      .spyOn(api, 'post')
      .mockResolvedValue({ status: 'approved' } as never);

    render(<CliAuthPage />);

    const approve = await screen.findByRole('button', { name: /approve/i });
    expect(approve).toBeDisabled();

    fireEvent.change(
      screen.getByLabelText(/verification code from your terminal/i),
      { target: { value: ' abcd-1234 ' } }
    );
    expect(approve).toBeEnabled();
    fireEvent.click(approve);

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('/cli-auth/approve', {
        session_id: 'session-1',
        verification_code: 'ABCD-1234',
      })
    );
  });

  it('clears the stored code once the login is approved', async () => {
    mockSearchParams = new URLSearchParams('session_id=session-1');
    window.sessionStorage.setItem('nous:cli-auth-code:session-1', 'WXYZ-9876');
    vi.spyOn(api, 'post').mockResolvedValue({ status: 'approved' } as never);

    render(<CliAuthPage />);

    fireEvent.click(await screen.findByRole('button', { name: /approve/i }));

    await screen.findByText(/cli connected/i);
    expect(window.sessionStorage.getItem('nous:cli-auth-code:session-1')).toBe(
      null
    );
  });
});
