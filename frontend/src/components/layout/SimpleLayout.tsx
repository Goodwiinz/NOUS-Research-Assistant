'use client';

import { useAuth } from '@/hooks/useAuth';
import { cn } from '@/lib/utils';
import { ChevronDown, LogOut, Settings } from 'lucide-react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
  type ReactElement,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';

interface SimpleLayoutProps {
  children: ReactNode;
  showHeader?: boolean;
}

const UI = { fontFamily: 'var(--nous-font-ui)' } as const;

// House focus ring (HeroSection). Theme tokens rather than the landing's fixed
// erebus/selene, because this shell follows the app theme.
const RING =
  'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50';
const RING_OFFSET = `${RING} focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-bg-1)`;

const PUBLIC_NAV = [
  { href: '/', label: 'Home' },
  { href: '/arxiv', label: 'arXiv' },
] as const;

// Signed-in only: for a signed-out visitor each of these is a redirect to
// the sign-in page.
const GATED_NAV = [
  { href: '/search', label: 'Search' },
  { href: '/chat', label: 'Chat' },
  { href: '/documents/upload', label: 'Documents' },
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/analytics', label: 'Analytics' },
] as const;

const MENU_ITEM = cn(
  'flex w-full items-center gap-2 px-3 py-2 text-sm text-(--nous-fg-2) transition-colors hover:bg-(--nous-bg-3) hover:text-(--nous-fg-1)',
  RING
);

/**
 * Shell for signed-out visitors on public routes (app/(public)/layout.tsx).
 * Signed-in users get the dashboard shell instead, so the gated links and the
 * user menu here only show if the client session resolves after the server
 * check.
 */
export function SimpleLayout({
  children,
  showHeader = true,
}: SimpleLayoutProps): ReactElement {
  const { user, isAuthenticated, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const handleLogout = (): void => {
    logout();
    router.push('/login');
  };

  // Close menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent): void => {
      if (
        userMenuRef.current &&
        !userMenuRef.current.contains(event.target as Node)
      ) {
        setUserMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  if (!showHeader) {
    return <>{children}</>;
  }

  const navItems = isAuthenticated ? [...PUBLIC_NAV, ...GATED_NAV] : PUBLIC_NAV;
  const isCurrent = (href: string): boolean =>
    href === '/'
      ? pathname === '/'
      : pathname === href || (pathname?.startsWith(`${href}/`) ?? false);

  return (
    <div className="min-h-screen bg-(--nous-bg-1) text-(--nous-fg-1)">
      <a
        href="#main-content"
        className={cn(
          'sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-3 focus:z-60 focus:rounded-(--nous-radius-md) focus:bg-(--nous-fg-1) focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-(--nous-bg-1)',
          RING_OFFSET
        )}
        style={UI}
      >
        Skip to main content
      </a>

      <header
        className="sticky top-0 z-50 border-b border-(--nous-border-1) bg-(--nous-bg-1)"
        style={UI}
      >
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-2 min-h-14 flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
          <Link
            href="/"
            className={cn('flex flex-col leading-none rounded-sm', RING)}
          >
            <span className="text-[15px] lg:text-base font-bold tracking-[0.18em] text-(--nous-fg-1)">
              NOUS
            </span>
            <span className="hidden sm:block text-[11px] tracking-[0.12em] text-(--nous-fg-2) mt-1">
              Multimodal Intelligence
            </span>
          </Link>

          <nav
            aria-label="Main"
            className="flex flex-wrap items-center gap-x-4 sm:gap-x-6 gap-y-2 text-sm"
          >
            {navItems.map((item) => {
              const current = isCurrent(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={current ? 'page' : undefined}
                  className={cn(
                    'inline-flex items-center min-h-9 rounded-sm transition-colors',
                    current
                      ? 'font-medium text-(--nous-fg-1) underline decoration-(--nous-sol) decoration-2 underline-offset-[6px]'
                      : 'text-(--nous-fg-2) hover:text-(--nous-fg-1)',
                    RING
                  )}
                >
                  {item.label}
                </Link>
              );
            })}

            {isAuthenticated ? (
              <div className="relative" ref={userMenuRef}>
                <button
                  type="button"
                  aria-label="Account menu"
                  aria-haspopup="true"
                  aria-expanded={userMenuOpen}
                  aria-controls={userMenuOpen ? menuId : undefined}
                  onClick={() => setUserMenuOpen(!userMenuOpen)}
                  className={cn(
                    'inline-flex h-9 items-center gap-2 rounded-(--nous-radius-md) border border-(--nous-border-2) px-2.5 text-(--nous-fg-1) transition-colors hover:bg-(--nous-bg-3)',
                    RING
                  )}
                >
                  <span className="max-w-[16ch] truncate text-sm">
                    {user?.email?.split('@')[0] || 'Account'}
                  </span>
                  <ChevronDown
                    aria-hidden="true"
                    className={cn(
                      'h-3.5 w-3.5 text-(--nous-fg-2) transition-transform',
                      userMenuOpen && 'rotate-180'
                    )}
                  />
                </button>

                {userMenuOpen && (
                  <div
                    id={menuId}
                    className="absolute right-0 top-full mt-2 w-56 overflow-hidden rounded-(--nous-radius-md) border border-(--nous-border-1) bg-(--nous-bg-2) shadow-(--nous-shadow-lg)"
                  >
                    <p className="truncate border-b border-(--nous-border-1) px-3 py-2 text-xs text-(--nous-fg-2)">
                      {user?.email}
                    </p>
                    <div className="py-1">
                      <Link
                        href="/settings"
                        onClick={() => setUserMenuOpen(false)}
                        className={MENU_ITEM}
                      >
                        <Settings aria-hidden="true" className="h-4 w-4" />
                        Settings
                      </Link>
                    </div>
                    <div className="border-t border-(--nous-border-1) py-1">
                      <button
                        type="button"
                        onClick={() => {
                          setUserMenuOpen(false);
                          handleLogout();
                        }}
                        className={MENU_ITEM}
                      >
                        <LogOut aria-hidden="true" className="h-4 w-4" />
                        Sign out
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <>
                <Link
                  href="/login"
                  className={cn(
                    'inline-flex items-center min-h-9 rounded-sm text-(--nous-fg-2) hover:text-(--nous-fg-1) transition-colors',
                    RING
                  )}
                >
                  Sign in
                </Link>
                <Link
                  href="/register"
                  className={cn(
                    'inline-flex items-center h-9 px-4 rounded-(--nous-radius-md) font-semibold bg-(--nous-fg-1) text-(--nous-bg-1) transition-opacity hover:opacity-90',
                    RING_OFFSET
                  )}
                >
                  Get started
                </Link>
              </>
            )}
          </nav>
        </div>
      </header>

      <main id="main-content" className="flex-1">
        {children}
      </main>
    </div>
  );
}
