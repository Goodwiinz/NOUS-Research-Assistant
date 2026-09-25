import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  isAuthenticated: true,
  push: vi.fn(),
  navigationLinks: undefined as unknown,
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: mocks.isAuthenticated,
    logout: vi.fn(),
  }),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/',
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock('@/components/ui/shadcn-io/navbar-02', () => ({
  Navbar02: (props: { navigationLinks: unknown }) => {
    mocks.navigationLinks = props.navigationLinks;
    return null;
  },
}));

import { AppNavbar } from '../../../../app/components/AppNavbar';

describe('AppNavbar', () => {
  it('orders workspace destinations by the primary user task flow', () => {
    render(<AppNavbar />);

    const links = mocks.navigationLinks as Array<{
      label: string;
      items?: Array<{ label: string }>;
    }>;
    expect(links[1]?.label).toBe('Workspace');
    expect(links[1]?.items?.map((item) => item.label)).toEqual([
      'Chat',
      'Semantic Search',
      'Documents',
    ]);
  });
});
