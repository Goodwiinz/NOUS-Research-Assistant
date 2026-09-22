import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import LoginPage from '../../../../app/(auth)/login/page';
import { downloadStoredNousCliAuth } from '@/services/nousCliAuth';

const mockLogin = vi.fn();
const mockPush = vi.fn();
let mockSearchParams = new URLSearchParams();

const mockedAuth = {
  login: mockLogin,
  isAuthenticated: false,
  isLoading: false,
};

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockedAuth,
}));

vi.mock('@/services/nousCliAuth', () => ({
  downloadStoredNousCliAuth: vi.fn(),
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

describe('App login page', () => {
  beforeEach(() => {
    mockLogin.mockReset();
    mockPush.mockReset();
    mockSearchParams = new URLSearchParams();
    mockedAuth.isAuthenticated = false;
    mockedAuth.isLoading = false;
    (downloadStoredNousCliAuth as Mock).mockReset();
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation(() => ({
        matches: false,
        media: '(prefers-reduced-motion: reduce)',
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  });

  it('downloads NOUS CLI auth when selected before sign in', async () => {
    mockLogin.mockResolvedValue(undefined);

    render(<LoginPage />);

    const checkbox = await screen.findByLabelText(
      /also download a nous cli credential/i
    );
    fireEvent.change(screen.getByTestId('email-input'), {
      target: { value: 'admin@multimodal-rag.com' },
    });
    fireEvent.change(screen.getByTestId('password-input'), {
      target: { value: 'secret-password' },
    });
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByTestId('login-button'));

    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith(
        'admin@multimodal-rag.com',
        'secret-password'
      )
    );
    expect(downloadStoredNousCliAuth).toHaveBeenCalledTimes(1);
  });

  it('does not download NOUS CLI auth unless selected', async () => {
    mockLogin.mockResolvedValue(undefined);

    render(<LoginPage />);

    await screen.findByTestId('email-input');
    fireEvent.change(screen.getByTestId('email-input'), {
      target: { value: 'admin@multimodal-rag.com' },
    });
    fireEvent.change(screen.getByTestId('password-input'), {
      target: { value: 'secret-password' },
    });
    fireEvent.click(screen.getByTestId('login-button'));

    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith(
        'admin@multimodal-rag.com',
        'secret-password'
      )
    );
    expect(downloadStoredNousCliAuth).not.toHaveBeenCalled();
  });

  it('redirects to the requested next path after login', async () => {
    mockLogin.mockResolvedValue(undefined);
    mockSearchParams = new URLSearchParams(
      'next=%2Fcli-auth%3Fsession_id%3Dsession-1%26code%3DABCD-1234'
    );

    render(<LoginPage />);

    await screen.findByTestId('email-input');
    fireEvent.change(screen.getByTestId('email-input'), {
      target: { value: 'admin@multimodal-rag.com' },
    });
    fireEvent.change(screen.getByTestId('password-input'), {
      target: { value: 'secret-password' },
    });
    fireEvent.click(screen.getByTestId('login-button'));

    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith(
        'admin@multimodal-rag.com',
        'secret-password'
      )
    );
    expect(mockPush).toHaveBeenCalledWith(
      '/cli-auth?session_id=session-1&code=ABCD-1234'
    );
  });

  it('shows neutral same-account guidance when a chat draft was saved', async () => {
    mockSearchParams = new URLSearchParams(
      'reauth=chat&draft=saved&next=%2Fchat%3Fthread%3Dthread-A&message=attacker-copy'
    );

    render(<LoginPage />);

    expect(
      await screen.findByText(
        "Please sign in again to continue. Sign in to the same account to return to this conversation. Your message text is saved in this tab and won't be sent until you press Send."
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/session expired/i)).not.toBeInTheDocument();
    expect(screen.queryByText('attacker-copy')).not.toBeInTheDocument();
  });

  it('shows generic recovery guidance when no chat draft was saved', async () => {
    mockSearchParams = new URLSearchParams(
      'reauth=chat&next=%2Fchat%3Fthread%3Dthread-A'
    );

    render(<LoginPage />);

    expect(
      await screen.findByText('Please sign in again to continue.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/session expired/i)).not.toBeInTheDocument();
  });

  it.each([
    ['//evil.com', '/dashboard'],
    ['http://evil.com', '/dashboard'],
    ['', '/dashboard'],
  ])(
    'blocks open redirect for next=%s and falls back to /dashboard',
    async (maliciousNext, expectedPath) => {
      mockLogin.mockResolvedValue(undefined);
      mockSearchParams = new URLSearchParams(
        `next=${encodeURIComponent(maliciousNext)}`
      );

      render(<LoginPage />);

      await screen.findByTestId('email-input');
      fireEvent.change(screen.getByTestId('email-input'), {
        target: { value: 'admin@multimodal-rag.com' },
      });
      fireEvent.change(screen.getByTestId('password-input'), {
        target: { value: 'secret-password' },
      });
      fireEvent.click(screen.getByTestId('login-button'));

      await waitFor(() => expect(mockLogin).toHaveBeenCalled());
      expect(mockPush).toHaveBeenCalledWith(expectedPath);
    }
  );
});
