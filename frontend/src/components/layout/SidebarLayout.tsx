'use client';

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb';
import { usePathname } from 'next/navigation';
import * as React from 'react';
import { AppRail, MobileNavigation } from './AppRail';
import { GlobalJobCenter } from './GlobalJobCenter';
import { ThemeToggle } from '@/components/ui/theme-toggle';
import { useProjectStore } from '@/store/projectStore';

interface SidebarLayoutProps {
  children: React.ReactNode;
  rightPanel?: React.ReactNode;
  showBreadcrumb?: boolean;
  showHeader?: boolean;
}

const pathNameMap: Record<string, string> = {
  '': 'Home',
  dashboard: 'Overview',
  search: 'Search',
  documents: 'Documents',
  upload: 'Upload',
  arxiv: 'ArXiv Papers',
  analytics: 'Analytics',
  diagnostics: 'Diagnostics',
  entities: 'Entities',
  projects: 'Projects',
  research: 'Research',
  'research-engine': 'Research Engine',
  realtime: 'Real-time',
  settings: 'Settings',
  team: 'Team',
  secrets: 'API Keys',
  notifications: 'Notifications',
  help: 'Help Center',
  login: 'Sign In',
  register: 'Register',
  chat: 'Chat',
  new: 'New',
  'quality-metrics-demo': 'Quality Metrics',
};

export function SidebarLayout({
  children,
  rightPanel,
  showBreadcrumb = true,
  showHeader = true,
}: SidebarLayoutProps): React.ReactElement {
  const pathname = usePathname();
  const currentProject = useProjectStore((s) => s.currentProject);

  const pathSegments = pathname?.split('/').filter(Boolean) || [];
  const breadcrumbItems = pathSegments.map((segment, index) => {
    const path = '/' + pathSegments.slice(0, index + 1).join('/');
    const isProjectId =
      index > 0 &&
      pathSegments[index - 1] === 'projects' &&
      !pathNameMap[segment];
    const name = isProjectId
      ? currentProject?.name || 'Project'
      : pathNameMap[segment] ||
        segment.charAt(0).toUpperCase() + segment.slice(1);
    const isLast = index === pathSegments.length - 1;
    return { path, name, isLast };
  });

  return (
    <div className="flex h-svh overflow-hidden">
      <AppRail />
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <div className="flex-1 flex flex-col min-w-0 min-h-0">
          <div className="flex h-14 shrink-0 items-center border-b border-(--nous-border-1) bg-(--nous-bg-1) px-3 md:hidden">
            <MobileNavigation />
          </div>
          {showHeader && (
            <header className="flex h-12 sm:h-14 shrink-0 items-center gap-3 border-b border-(--nous-border-1) bg-(--nous-bg-1)/95 backdrop-blur-sm px-3 sm:px-4">
              {showBreadcrumb && (
                <Breadcrumb>
                  <BreadcrumbList className="text-xs">
                    <BreadcrumbItem>
                      {pathname === '/dashboard' ? (
                        <BreadcrumbPage className="text-primary">
                          Dashboard
                        </BreadcrumbPage>
                      ) : (
                        <BreadcrumbLink
                          href="/dashboard"
                          className="text-muted-foreground hover:text-foreground transition-colors"
                        >
                          Dashboard
                        </BreadcrumbLink>
                      )}
                    </BreadcrumbItem>
                    {breadcrumbItems
                      .filter((item) => item.path !== '/dashboard')
                      .map((item) => (
                        <React.Fragment key={item.path}>
                          <BreadcrumbSeparator className="text-muted-foreground/50">
                            /
                          </BreadcrumbSeparator>
                          <BreadcrumbItem>
                            {item.isLast ? (
                              <BreadcrumbPage className="text-primary">
                                {item.name}
                              </BreadcrumbPage>
                            ) : (
                              <BreadcrumbLink
                                href={item.path}
                                className="text-muted-foreground hover:text-foreground transition-colors"
                              >
                                {item.name}
                              </BreadcrumbLink>
                            )}
                          </BreadcrumbItem>
                        </React.Fragment>
                      ))}
                  </BreadcrumbList>
                </Breadcrumb>
              )}

              <div className="ml-auto flex items-center gap-2">
                <ThemeToggle />
                <GlobalJobCenter />
              </div>
            </header>
          )}

          <div className="flex-1 overflow-auto">{children}</div>
        </div>

        {rightPanel}
      </div>
    </div>
  );
}
