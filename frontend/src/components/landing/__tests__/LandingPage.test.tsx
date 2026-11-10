import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';

import HomePage from '../../../../app/page';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ isAuthenticated: false }),
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

describe('landing page structure', () => {
  it('starts with a skip link to <main>', () => {
    const { container } = render(<HomePage />);
    const first = container.querySelector('a, button');
    expect(first).toHaveTextContent(/skip to main content/i);
    expect(first).toHaveAttribute('href', '#main-content');
    expect(first?.className).toContain('focus-visible:ring-2');
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  });

  it('puts nav, main and footer side by side with the hero inside main', () => {
    const { container } = render(<HomePage />);
    const root = container.firstElementChild as HTMLElement;
    const landmarks = Array.from(root.children)
      .map((el) => el.tagName.toLowerCase())
      .filter((tag) => ['nav', 'main', 'footer'].includes(tag));
    expect(landmarks).toEqual(['nav', 'main', 'footer']);
    const main = screen.getByRole('main');
    expect(main.querySelector('h1')).toHaveTextContent(/knowledge that/i);
    expect(main.querySelector('#what')).not.toBeNull();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });
});
