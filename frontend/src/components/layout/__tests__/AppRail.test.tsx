import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  logout: vi.fn(),
  push: vi.fn(),
  pathname: '/dashboard',
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    user: { email: 'researcher@example.com' },
    logout: mocks.logout,
  }),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => mocks.pathname,
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock('next/link', () => ({
  default: ({
    href,
    children,
    ...props
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock('@/components/notifications/BellPopover', () => ({
  BellPopover: () => <button aria-label="Notifications" />,
}));

import { AppRail } from '../AppRail';

describe('AppRail', () => {
  it('keeps primary navigation in the same order as the sidebar sections', () => {
    render(<AppRail />);

    const primary = screen.getByRole('complementary', { name: 'Primary' });
    const expectedOrder = [
      'Overview',
      'Chat',
      'Search',
      'Documents',
      'Upload',
      'ArXiv Papers',
      'Knowledge graph',
      'Research',
      'Research Engine',
      'Analytics',
      'Diagnostics',
      'Notifications',
      'Settings',
    ];

    const actualOrder = Array.from(primary.querySelectorAll('a,button'))
      .map((link) => link.getAttribute('aria-label'))
      .filter((label): label is string => expectedOrder.includes(label));

    expect(actualOrder).toEqual(expectedOrder);
  });

  it('exposes the same three semantic navigation sections as the expanded sidebar', () => {
    render(<AppRail />);

    const primary = screen.getByRole('complementary', { name: 'Primary' });
    expect(
      within(primary).getByRole('group', { name: 'Main' })
    ).toBeInTheDocument();
    expect(
      within(primary).getByRole('group', { name: 'Knowledge' })
    ).toBeInTheDocument();
    expect(
      within(primary).getByRole('group', { name: 'System' })
    ).toBeInTheDocument();
  });

  it('lets the user sign out from the rail', () => {
    render(<AppRail />);

    fireEvent.click(screen.getByRole('button', { name: /sign out/i }));

    expect(mocks.logout).toHaveBeenCalledTimes(1);
    expect(mocks.push).toHaveBeenCalledWith('/login');
  });
});
