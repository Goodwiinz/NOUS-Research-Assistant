import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import {
  fireEvent,
  render,
  screen,
  within,
  type RenderResult,
} from '@testing-library/react';
import type { ReactNode } from 'react';

import { SimpleLayout } from '@/components/layout/SimpleLayout';

const mockedAuth = {
  user: null as { email: string } | null,
  isAuthenticated: false,
  logout: vi.fn(),
};
let mockPathname = '/arxiv';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockedAuth,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => mockPathname,
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

const GATED = [
  '/search',
  '/chat',
  '/documents/upload',
  '/dashboard',
  '/analytics',
];

function renderShell(): RenderResult {
  return render(
    <SimpleLayout>
      <h1>arXiv papers</h1>
    </SimpleLayout>
  );
}

describe('SimpleLayout (signed-out shell)', () => {
  beforeEach(() => {
    mockedAuth.user = null;
    mockedAuth.isAuthenticated = false;
    mockPathname = '/arxiv';
  });

  it('renders the NOUS wordmark linking home and no heading of its own', () => {
    renderShell();
    const home = screen.getByRole('link', { name: /NOUS/ });
    expect(home).toHaveAttribute('href', '/');
    expect(home).toHaveTextContent('Multimodal Intelligence');
    expect(screen.queryByText(/RAG System|Terminal Observatory/)).toBeNull();
    // Only the page's own <h1> remains.
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('gives a signed-out visitor home, arXiv, sign in and get started, and nothing gated', () => {
    const { container } = renderShell();
    const nav = screen.getByRole('navigation', { name: /main/i });
    expect(nav.className).not.toMatch(/(^|\s)hidden(\s|$)/);
    expect(within(nav).getByRole('link', { name: 'Home' })).toHaveAttribute(
      'href',
      '/'
    );
    expect(within(nav).getByRole('link', { name: 'arXiv' })).toHaveAttribute(
      'href',
      '/arxiv'
    );
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute(
      'href',
      '/login'
    );
    expect(screen.getByRole('link', { name: 'Get started' })).toHaveAttribute(
      'href',
      '/register'
    );

    const hrefs = Array.from(container.querySelectorAll('a')).map((a) =>
      a.getAttribute('href')
    );
    GATED.forEach((href) => expect(hrefs).not.toContain(href));
    expect(hrefs).not.toContain('#');
  });

  it('shows the gated areas and the user menu when signed in', () => {
    mockedAuth.isAuthenticated = true;
    mockedAuth.user = { email: 'ada@example.com' };
    const { container } = renderShell();
    const hrefs = Array.from(container.querySelectorAll('a')).map((a) =>
      a.getAttribute('href')
    );
    GATED.forEach((href) => expect(hrefs).toContain(href));
    expect(screen.queryByRole('link', { name: 'Sign in' })).toBeNull();

    const menuButton = screen.getByRole('button', { name: /account menu/i });
    expect(menuButton).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(menuButton);
    expect(menuButton).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/settings'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(mockedAuth.logout).toHaveBeenCalled();
  });

  it('marks only the current page with aria-current and a non-colour cue', () => {
    renderShell();
    const current = screen.getByRole('link', { name: 'arXiv' });
    expect(current).toHaveAttribute('aria-current', 'page');
    expect(current.className).toMatch(/underline|border-b/);
    expect(screen.getByRole('link', { name: 'Home' })).not.toHaveAttribute(
      'aria-current'
    );
  });

  it('starts with a skip link to a <main> landmark holding the page', () => {
    const { container } = renderShell();
    const first = container.querySelector('a, button');
    expect(first).toHaveTextContent(/skip to main content/i);
    expect(first).toHaveAttribute('href', '#main-content');
    const main = screen.getByRole('main');
    expect(main).toHaveAttribute('id', 'main-content');
    expect(within(main).getByRole('heading', { level: 1 })).toBeInTheDocument();
  });

  it('puts the house focus ring on every link and button and drops the terminal styling', () => {
    mockedAuth.isAuthenticated = true;
    mockedAuth.user = { email: 'ada@example.com' };
    const { container } = renderShell();
    fireEvent.click(screen.getByRole('button', { name: /account menu/i }));
    const controls = container.querySelectorAll('a, button');
    expect(controls.length).toBeGreaterThan(6);
    controls.forEach((el) => {
      expect(el.className).toContain('focus-visible:ring-2');
    });
    expect(container.innerHTML).not.toMatch(/font-mono|white\/\d|text-red-/);
  });
});
