import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';

import NotFound, { metadata } from '../../../../app/not-found';

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

describe('app/not-found', () => {
  it('has its own tab title, which needs a server component', () => {
    expect(metadata.title).toBe('Page not found | NOUS');
    const source = readFileSync(
      resolve(process.cwd(), 'app/not-found.tsx'),
      'utf8'
    );
    expect(source).not.toMatch(/^['"]use client['"]/m);
  });

  it('uses one sentence-case serif heading and no terminal styling', () => {
    const { container } = render(<NotFound />);
    const headings = screen.getAllByRole('heading', { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent('Page not found');
    expect(headings[0].getAttribute('style') ?? '').toContain(
      '--nous-font-body'
    );
    expect(container.innerHTML).not.toMatch(/font-mono|uppercase|shadow-\[0_0/);
    expect(container.textContent).not.toMatch(/PAGE NOT FOUND|GO TO DASHBOARD/);
  });

  it('leads home first and offers an honest dashboard link second', () => {
    render(<NotFound />);
    expect(screen.getByRole('link', { name: /^NOUS/ })).toHaveAttribute(
      'href',
      '/'
    );
    expect(screen.getByRole('link', { name: 'Back to home' })).toHaveAttribute(
      'href',
      '/'
    );
    const dashboard = screen.getByRole('link', { name: /dashboard/i });
    expect(dashboard).toHaveAttribute('href', '/dashboard');
    expect(dashboard).toHaveTextContent(/sign in/i);
    expect(screen.queryByRole('link', { name: /search/i })).toBeNull();
  });

  it('puts the house focus ring on every link', () => {
    const { container } = render(<NotFound />);
    container.querySelectorAll('a').forEach((a) => {
      expect(a.className).toContain('focus-visible:ring-2');
    });
  });
});
