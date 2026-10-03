'use client';

import type { Navbar02NavItem } from '@/components/ui/shadcn-io/navbar-02';
import { Navbar02 } from '@/components/ui/shadcn-io/navbar-02';
import { useAuth } from '@/hooks/useAuth';
import { usePathname, useRouter } from 'next/navigation';

export function AppNavbar() {
  const { isAuthenticated, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  // Hide navbar on dashboard routes
  if (
    pathname?.startsWith('/chat') ||
    pathname?.startsWith('/documents/upload')
  ) {
    return null;
  }

  const workspaceItems: Navbar02NavItem['items'] = [
    {
      href: '/chat',
      label: 'Chat',
      icon: 'MessageSquare',
      description: 'Ask questions and synthesize answers from your workspace.',
    },
    {
      href: '/search',
      label: 'Semantic Search',
      icon: 'Search',
      description:
        'Ask complex questions and retrieve answers with semantic understanding.',
    },
    {
      href: '/documents/upload',
      label: 'Documents',
      icon: 'FileText',
      description:
        'Ingest PDFs, images, audio, and video into your knowledge base.',
    },
  ];

  const accountItems: Navbar02NavItem['items'] = isAuthenticated
    ? [
        {
          href: '/documents/upload',
          label: 'Workspace Hub',
          icon: 'Upload',
          description:
            'Jump directly into managing and monitoring your documents.',
        },
      ]
    : [
        {
          href: '/login',
          label: 'Sign In',
          icon: 'LogIn',
          description:
            'Access your existing workspace and continue where you left off.',
        },
        {
          href: '/register',
          label: 'Create Account',
          icon: 'UserPlus',
          description: 'Set up a new organization workspace in minutes.',
        },
      ];

  const navLinks: Navbar02NavItem[] = [
    { href: '/', label: 'Home' },
    {
      label: 'Workspace',
      submenu: true,
      type: 'icon',
      items: workspaceItems,
    },
    ...(isAuthenticated
      ? []
      : [{ href: '/register', label: 'Get Started' as const }]),
    {
      label: 'Account',
      submenu: true,
      type: 'icon',
      items: accountItems,
    },
  ];

  return (
    <Navbar02
      logo={
        <div className="flex items-center gap-2 font-bold text-xl">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center text-primary-foreground shadow-xs">
            R
          </div>
          <span className="text-primary">RAG System</span>
        </div>
      }
      logoHref="/"
      ctaText={isAuthenticated ? 'Workspace' : 'Get Started'}
      ctaHref={isAuthenticated ? '/documents/upload' : '/register'}
      signInText={isAuthenticated ? 'Sign Out' : 'Login'}
      signInHref={isAuthenticated ? '#' : '/login'}
      onSignInClick={isAuthenticated ? logout : () => router.push('/login')}
      onCtaClick={() =>
        router.push(isAuthenticated ? '/documents/upload' : '/register')
      }
      navigationLinks={navLinks}
    />
  );
}
