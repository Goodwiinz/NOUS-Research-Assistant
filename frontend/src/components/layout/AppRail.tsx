'use client';

import { useAuth } from '@/hooks/useAuth';
import { cn } from '@/lib/utils';
import type { LucideIcon } from 'lucide-react';
import {
  Activity,
  BarChart3,
  BookOpen,
  FileText,
  FlaskConical,
  LayoutGrid,
  LogOut,
  MessageSquare,
  Network,
  Search,
  Settings,
  Upload,
  Workflow,
} from 'lucide-react';
import Link from 'next/link';
import { BellPopover } from '@/components/notifications/BellPopover';
import { usePathname, useRouter } from 'next/navigation';

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
}: RailButtonProps) {
  return (
    <Link
      href={href}
      aria-label={tip}
      aria-current={active ? 'page' : undefined}
      data-tip={tip}
      className={cn(
        'rail-btn relative flex items-center justify-center w-9 h-9 rounded-lg transition-colors duration-150',
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

export function AppRail() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, logout } = useAuth();

  const isActive = (url: string) => {
    if (url === '/dashboard') return pathname === '/dashboard';
    return pathname?.startsWith(url) ?? false;
  };

  const getInitials = (email: string | undefined) => {
    if (!email) return 'U';
    return email.split('@')[0].slice(0, 2).toUpperCase();
  };

  const handleLogout = () => {
    logout();
    router.push('/login');
  };

  return (
    <aside
      aria-label="Primary"
      className="flex flex-col items-center w-14 h-full shrink-0 py-3 gap-1 border-r border-(--nous-border-1) bg-(--nous-bg-2) dark:bg-(--nous-nyx) dark:border-(--nous-shade)"
    >
      {/* Brand mark */}
      <Link
        href="/dashboard"
        aria-label="NOUS home"
        className="relative flex items-center justify-center w-8 h-8 rounded-lg bg-(--nous-erebus) dark:bg-(--nous-sol) shadow-xs mb-1"
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

      <nav
        aria-label="Primary navigation"
        className="flex flex-col items-center gap-1"
      >
        <div
          role="group"
          aria-label="Main"
          className="flex flex-col items-center gap-1"
        >
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
            icon={Search}
            tip="Search"
            href="/search"
            active={isActive('/search')}
          />
        </div>

        <div className="w-[22px] h-px bg-(--nous-border-1) dark:bg-(--nous-shade) my-1.5" />

        <div
          role="group"
          aria-label="Knowledge"
          className="flex flex-col items-center gap-1"
        >
          <RailButton
            icon={FileText}
            tip="Documents"
            href="/documents"
            active={isActive('/documents')}
          />
          <RailButton
            icon={Upload}
            tip="Upload"
            href="/documents/upload"
            active={isActive('/documents/upload')}
          />
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
        </div>

        <div className="w-[22px] h-px bg-(--nous-border-1) dark:bg-(--nous-shade) my-1.5" />

        <div
          role="group"
          aria-label="System"
          className="flex flex-col items-center gap-1"
        >
          <RailButton
            icon={BarChart3}
            tip="Analytics"
            href="/analytics"
            active={isActive('/analytics')}
          />
          <RailButton
            icon={Activity}
            tip="Diagnostics"
            href="/diagnostics"
            active={isActive('/diagnostics')}
          />
          <BellPopover active={isActive('/notifications')} />
          <RailButton
            icon={Settings}
            tip="Settings"
            href="/settings"
            active={isActive('/settings')}
          />
        </div>
      </nav>

      {/* Spacer */}
      <div className="flex-1" />

      <button
        type="button"
        aria-label="Sign out"
        data-tip="Sign out"
        onClick={handleLogout}
        className={cn(
          'rail-btn relative flex items-center justify-center w-9 h-9 rounded-lg transition-colors duration-150',
          'text-red-400 hover:text-red-300 hover:bg-red-500/10 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-red-400/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-bg-2) dark:focus-visible:ring-offset-(--nous-nyx)'
        )}
      >
        <LogOut className="w-4 h-4 rail-icon" />
      </button>

      {/* User avatar */}
      <Link
        href="/settings"
        aria-label="Account"
        className="relative flex items-center justify-center w-8 h-8 rounded-full bg-linear-to-br from-(--nous-sol) to-(--nous-helios) shadow-[0_0_0_2px_var(--nous-bg-2),0_2px_4px_rgba(212,160,57,0.2)] dark:shadow-[0_0_0_2px_var(--nous-nyx),0_2px_4px_rgba(212,160,57,0.2)]"
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
