import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { api } from '@/services/api-client';

import CliAuthPage from '../../../../app/(auth)/cli-auth/page';

const mockPush = vi.fn();

const mockedAuth = {
  isAuthenticated: true,
  isLoading: false,
};

let mockSearchParams = new URLSearchParams('session_id=session-1');

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

const PENDING = {
  session_id: 'session-1',
  status: 'pending',
  started_at: '2026-10-07T08:00:00Z',
  expires_at: '2026-10-07T08:05:00Z',
  requester_ip: '203.0.113.7',
  requester_user_agent: 'nous-cli/1.4.0 (darwin; arm64)',
};

function codeInput(): HTMLInputElement {
  return screen.getByLabelText(/code from your terminal/i) as HTMLInputElement;
}

describe('CliAuthPage', () => {
  beforeEach(() => {
    mockPush.mockReset();
    vi.restoreAllMocks();
    mockedAuth.isAuthenticated = true;
    mockedAuth.isLoading = false;
    mockSearchParams = new URLSearchParams('session_id=session-1');
    window.sessionStorage.clear();
    window.history.replaceState(null, '', '/');
  });

  it('shows the requester and a warning, and never pre-fills a code from the link', async () => {
    mockSearchParams = new URLSearchParams(
      'session_id=session-1&code=ABCD-1234'
    );
    window.history.replaceState(
      null,
      '',
      '/cli-auth?session_id=session-1&code=ABCD-1234#code=WXYZ-9876'
    );
    window.sessionStorage.setItem('nous:cli-auth-code:session-1', 'QRST-5555');
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue(PENDING as never);

    render(<CliAuthPage />);

    expect(await screen.findByText('203.0.113.7')).toBeInTheDocument();
    expect(
      screen.getByText('nous-cli/1.4.0 (darwin; arm64)')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /only approve if you started this from your own terminal/i
      )
    ).toBeInTheDocument();
    expect(getSpy).toHaveBeenCalledWith('/cli-auth/session/session-1');
    expect(codeInput().value).toBe('');
    expect(screen.queryByText(/ABCD-1234|WXYZ-9876|QRST-5555/)).toBeNull();
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();
    expect(window.location.href).not.toMatch(/ABCD|WXYZ/);
    expect(
      window.sessionStorage.getItem('nous:cli-auth-code:session-1')
    ).toBeNull();
  });

  it('enables Approve only after eight characters and submits the normalized code', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(PENDING as never);
    const postSpy = vi
      .spyOn(api, 'post')
      .mockResolvedValue({ status: 'approved' } as never);

    render(<CliAuthPage />);
    await screen.findByText('203.0.113.7');
    const approve = screen.getByRole('button', { name: /approve/i });

    fireEvent.change(codeInput(), { target: { value: 'abcd-123' } });
    expect(approve).toBeDisabled();

    fireEvent.change(codeInput(), { target: { value: ' abcd-1234 ' } });
    expect(codeInput().value).toBe('ABCD-1234');
    expect(approve).toBeEnabled();

    fireEvent.click(approve);

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

  it('shows the server error when the typed code does not match', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(PENDING as never);
    vi.spyOn(api, 'post').mockRejectedValue(
      new Error(
        'Verification code does not match the one shown in your terminal'
      )
    );

    render(<CliAuthPage />);
    await screen.findByText('203.0.113.7');
    fireEvent.change(codeInput(), { target: { value: 'ZZZZ9999' } });
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /does not match/i
    );
  });

  it('keeps Approve disabled when the request details cannot be loaded', async () => {
    vi.spyOn(api, 'get').mockRejectedValue(new Error('Not Found'));

    render(<CliAuthPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /not found or has expired/i
    );
    fireEvent.change(codeInput(), { target: { value: 'ABCD1234' } });
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();
  });

  it('keeps Approve disabled for a request that is no longer pending', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      ...PENDING,
      status: 'expired',
    } as never);

    render(<CliAuthPage />);

    expect(await screen.findByText(/no longer waiting/i)).toBeInTheDocument();
    fireEvent.change(codeInput(), { target: { value: 'ABCD1234' } });
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();
  });

  it('redirects to login without any code in the next URL', async () => {
    mockedAuth.isAuthenticated = false;
    mockSearchParams = new URLSearchParams(
      'session_id=session-1&code=ABCD-1234'
    );
    const getSpy = vi.spyOn(api, 'get');

    render(<CliAuthPage />);

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith(
        `/login?next=${encodeURIComponent('/cli-auth?session_id=session-1')}`
      )
    );
    expect(mockPush.mock.calls.flat().join(' ')).not.toContain('ABCD');
    expect(getSpy).not.toHaveBeenCalled();
  });
  it('shows an error instead of loading forever when the link has no session id', async () => {
    mockSearchParams = new URLSearchParams('');
    const getSpy = vi.spyOn(api, 'get');

    render(<CliAuthPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /missing its sign-in request/i
    );
    expect(screen.queryByText('Loading…')).toBeNull();
    expect(getSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();
  });

  it('enables Approve after pasting a code copied with a leading space', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(PENDING as never);
    const user = userEvent.setup();

    render(<CliAuthPage />);
    await screen.findByText('203.0.113.7');
    await user.click(codeInput());
    await user.paste(' ABCD-1234');

    expect(codeInput().value).toBe('ABCD-1234');
    expect(screen.getByRole('button', { name: /approve/i })).toBeEnabled();
  });

  it('names the Approve button by its visible text', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(PENDING as never);

    render(<CliAuthPage />);
    await screen.findByText('203.0.113.7');

    expect(
      screen.getByRole('button', { name: 'Approve sign-in' })
    ).toBeInTheDocument();
  });

  it('labels requester details as reported and possibly inaccurate', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(PENDING as never);

    render(<CliAuthPage />);
    await screen.findByText('203.0.113.7');

    expect(
      screen.getByText(/device \(as reported by the requester\)/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/may be inaccurate/i)).toBeInTheDocument();
  });
});
