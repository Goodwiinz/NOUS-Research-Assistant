'use client';

import { useAuth } from '@/hooks/useAuth';
import { cn } from '@/lib/utils';
import type { LucideIcon } from 'lucide-react';
import {
  BarChart3,
  Bell,
  BookOpen,
  FileText,
  FlaskConical,
  LayoutGrid,
  LogOut,
  MessageSquare,
  Network,
  Search,
  Settings,
  Workflow,
} from 'lucide-react';
import Link from 'next/link';
import { BellPopover } from '@/components/notifications/BellPopover';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DialogClose,
} from '@/components/ui/dialog';
import { usePathname, useRouter } from 'next/navigation';
import { useRef, type ReactElement } from 'react';

const mobileNavItems = [
  { label: 'Overview', href: '/dashboard', icon: LayoutGrid },
  { label: 'Chat', href: '/chat', icon: MessageSquare },
  { label: 'Documents', href: '/documents', icon: FileText },
  { label: 'Search', href: '/search', icon: Search },
  { label: 'ArXiv Papers', href: '/arxiv', icon: BookOpen },
  { label: 'Knowledge graph', href: '/entities', icon: Network },
  { label: 'Research', href: '/research', icon: FlaskConical },
  { label: 'Research Engine', href: '/research-engine', icon: Workflow },
  { label: 'Analytics', href: '/analytics', icon: BarChart3 },
  { label: 'Notifications', href: '/notifications', icon: Bell },
  { label: 'Settings', href: '/settings', icon: Settings },
] as const;

interface RailButtonProps {
  icon: LucideIcon;
  tip: string;
  href: string;
  active?: boolean;
  badge?: number;
  dot?: boolean;
}

function RailButton({
  icon: Icon,
  tip,
  href,
  active,
  badge,
  dot,
}: RailButtonProps): ReactElement {
  return (
    <Link
      href={href}
      aria-label={tip}
      aria-current={active ? 'page' : undefined}
      data-tip={tip}
      className={cn(
        'rail-btn relative flex shrink-0 items-center justify-center w-9 h-9 rounded-lg transition-colors duration-150',
        active
          ? 'bg-(--nous-aurum) text-(--nous-sol-safe) dark:bg-(--nous-ember) dark:text-(--nous-helios)'
          : 'text-(--nous-fg-3) hover:bg-(--nous-aurum) hover:text-(--nous-sol-safe) dark:hover:bg-(--nous-ember) dark:hover:text-(--nous-helios)'
      )}
    >
      {active && (
        <span className="absolute left-[-10px] top-2 bottom-2 w-0.5 rounded-r bg-(--nous-sol) dark:bg-(--nous-helios)" />
      )}
      <Icon className="w-4 h-4 rail-icon" />
      {badge != null && badge > 0 && (
        <span className="absolute top-[3px] right-[2px] min-w-[14px] h-[14px] px-[3px] flex items-center justify-center rounded-full bg-(--nous-sol) text-white text-[9px] font-bold font-(--nous-font-mono) shadow-[0_0_0_2px_var(--nous-bg-2)] dark:shadow-[0_0_0_2px_var(--nous-nyx)]">
          {badge}
        </span>
      )}
      {dot && (
        <span className="absolute top-[7px] right-[7px] w-1.5 h-1.5 rounded-full bg-(--nous-sol) shadow-[0_0_0_2px_var(--nous-bg-2)] dark:shadow-[0_0_0_2px_var(--nous-nyx)]" />
      )}
    </Link>
  );
}

export function MobileNavigation(): ReactElement {
  const pathname = usePathname();
  const router = useRouter();
  const { logout } = useAuth();
  const firstNavLinkRef = useRef<HTMLAnchorElement>(null);

  const isActive = (url: string): boolean =>
    url === '/dashboard'
      ? pathname === '/dashboard'
      : (pathname?.startsWith(url) ?? false);
  const handleOpenAutoFocus = (event: Event): void => {
    event.preventDefault();
    firstNavLinkRef.current?.focus();
  };

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          aria-label="Open navigation"
          className="inline-flex min-h-11 items-center gap-2 rounded-md border border-(--nous-border-1) bg-(--nous-bg-2) px-3 text-xs font-medium text-(--nous-fg-2) transition-colors hover:bg-(--nous-bg-3) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)"
        >
          <LayoutGrid className="h-4 w-4" aria-hidden />
          Navigation
        </button>
      </DialogTrigger>
      <DialogContent
        onOpenAutoFocus={handleOpenAutoFocus}
        className="left-0 top-0 h-full w-[min(320px,88vw)] translate-x-0 translate-y-0 gap-0 overflow-y-auto rounded-none border-l-0 border-r border-(--nous-border-1) bg-(--nous-bg-1) p-0 text-(--nous-fg-1) sm:max-w-none"
      >
        <DialogHeader className="border-b border-(--nous-border-1) px-5 py-4 text-left">
          <DialogTitle className="text-sm">Navigation</DialogTitle>
        </DialogHeader>
        <nav
          aria-label="Primary navigation"
          className="flex flex-col gap-1 p-3"
        >
          {mobileNavItems.map(({ label, href, icon: Icon }, index) => (
            <DialogClose asChild key={href}>
              <Link
                href={href}
                ref={index === 0 ? firstNavLinkRef : undefined}
                aria-current={isActive(href) ? 'page' : undefined}
                className={cn(
                  'flex min-h-11 items-center gap-3 rounded-md px-3 text-sm transition-colors',
                  isActive(href)
                    ? 'bg-(--nous-aurum) text-(--nous-sol-safe) dark:bg-(--nous-ember) dark:text-(--nous-helios)'
                    : 'text-(--nous-fg-2) hover:bg-(--nous-bg-3) hover:text-(--nous-fg-1)'
                )}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden />
                {label}
              </Link>
            </DialogClose>
          ))}
          <DialogClose asChild>
            <button
              type="button"
              onClick={() => {
                logout();
                router.push('/login');
              }}
              className="flex min-h-11 items-center gap-3 rounded-md px-3 text-left text-sm text-red-400 transition-colors hover:bg-red-500/10 hover:text-red-300"
            >
              <LogOut className="h-4 w-4 shrink-0" aria-hidden />
              Sign out
            </button>
          </DialogClose>
        </nav>
      </DialogContent>
    </Dialog>
  );
}

export function AppRail(): ReactElement {
  const pathname = usePathname();
  const router = useRouter();
  const { user, logout } = useAuth();

  const isActive = (url: string): boolean => {
    if (url === '/dashboard') return pathname === '/dashboard';
    return pathname?.startsWith(url) ?? false;
  };

  const getInitials = (email: string | undefined): string => {
    if (!email) return 'U';
    return email.split('@')[0].slice(0, 2).toUpperCase();
  };

  const handleLogout = (): void => {
    logout();
    router.push('/login');
  };

  return (
    <aside
      aria-label="Primary"
      className="hidden w-14 h-full shrink-0 flex-col items-center gap-1 border-r border-(--nous-border-1) bg-(--nous-bg-2) py-3 dark:border-(--nous-shade) dark:bg-(--nous-nyx) md:flex"
    >
      {/* Brand mark */}
      <Link
        href="/dashboard"
        aria-label="NOUS home"
        className="relative flex shrink-0 items-center justify-center w-8 h-8 rounded-lg bg-(--nous-erebus) dark:bg-(--nous-sol) shadow-xs mb-1"
      >
        <span
          className="text-white dark:text-(--nous-nyx) text-[15px] font-bold leading-none"
          style={{
            fontFamily: 'var(--nous-font-body)',
            letterSpacing: '-0.02em',
          }}
        >
          N
        </span>
        <span className="absolute inset-[-2px] rounded-[10px] border border-(--nous-aurum) dark:border-(--nous-ember) opacity-55 pointer-events-none" />
      </Link>

      {/* Divider */}
      <div className="w-[22px] h-px bg-(--nous-border-1) dark:bg-(--nous-shade) my-1.5" />

      {/* Navigation — hub */}
      <RailButton
        icon={LayoutGrid}
        tip="Overview"
        href="/dashboard"
        active={isActive('/dashboard')}
      />
      <RailButton
        icon={MessageSquare}
        tip="Chat"
        href="/chat"
        active={isActive('/chat')}
      />
      <RailButton
        icon={FileText}
        tip="Documents"
        href="/documents"
        active={isActive('/documents')}
      />
      <RailButton
        icon={Search}
        tip="Search"
        href="/search"
        active={isActive('/search')}
      />

      {/* Divider — knowledge */}
      <div className="w-[22px] h-px bg-(--nous-border-1) dark:bg-(--nous-shade) my-1.5" />

      <RailButton
        icon={BookOpen}
        tip="ArXiv Papers"
        href="/arxiv"
        active={isActive('/arxiv')}
      />
      <RailButton
        icon={Network}
        tip="Knowledge graph"
        href="/entities"
        active={isActive('/entities')}
      />
      <RailButton
        icon={FlaskConical}
        tip="Research"
        href="/research"
        active={isActive('/research')}
      />
      <RailButton
        icon={Workflow}
        tip="Research Engine"
        href="/research-engine"
        active={isActive('/research-engine')}
      />

      {/* Divider — system */}
      <div className="w-[22px] h-px bg-(--nous-border-1) dark:bg-(--nous-shade) my-1.5" />

      <RailButton
        icon={BarChart3}
        tip="Analytics"
        href="/analytics"
        active={isActive('/analytics')}
      />

      {/* Divider */}
      <div className="w-[22px] h-px bg-(--nous-border-1) dark:bg-(--nous-shade) my-1.5" />

      <BellPopover active={isActive('/notifications')} />
      <RailButton
        icon={Settings}
        tip="Settings"
        href="/settings"
        active={isActive('/settings')}
      />

      {/* Spacer */}
      <div className="flex-1" />

      <button
        type="button"
        aria-label="Sign out"
        data-tip="Sign out"
        onClick={handleLogout}
        className={cn(
          'rail-btn relative flex shrink-0 items-center justify-center w-9 h-9 rounded-lg transition-colors duration-150',
          'text-red-400 hover:text-red-300 hover:bg-red-500/10 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-red-400/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-bg-2) dark:focus-visible:ring-offset-(--nous-nyx)'
        )}
      >
        <LogOut className="w-4 h-4 rail-icon" />
      </button>

      {/* User avatar */}
      <Link
        href="/settings"
        aria-label="Account"
        className="relative flex shrink-0 items-center justify-center w-8 h-8 rounded-full bg-linear-to-br from-(--nous-sol) to-(--nous-helios) shadow-[0_0_0_2px_var(--nous-bg-2),0_2px_4px_rgba(212,160,57,0.2)] dark:shadow-[0_0_0_2px_var(--nous-nyx),0_2px_4px_rgba(212,160,57,0.2)]"
      >
        <span
          className="text-white text-[11px] font-semibold leading-none"
          style={{ fontFamily: 'var(--nous-font-ui)' }}
        >
          {getInitials(user?.email)}
        </span>
        <span className="absolute -bottom-px -right-px w-[9px] h-[9px] rounded-full bg-(--nous-terra) shadow-[0_0_0_2px_var(--nous-bg-2)] dark:shadow-[0_0_0_2px_var(--nous-nyx)]" />
      </Link>
    </aside>
  );
}
