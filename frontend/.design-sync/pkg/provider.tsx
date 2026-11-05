'use client';

// NousProvider - the root wrapper for previews and for designs built with the
// synced components (cfg.provider; merged into window.Nous via extraEntries).
// It is app/providers.tsx minus what needs the live backend (Statsig, toasts),
// plus the Next app-router contexts that `next/navigation` hooks read: outside
// a Next app those contexts are unset and `useRouter()` throws.
import React, { useState, type ReactNode } from 'react';
import { ThemeProvider } from 'next-themes';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppRouterContext } from 'next/dist/shared/lib/app-router-context.shared-runtime';
import {
  PathnameContext,
  PathParamsContext,
  SearchParamsContext,
} from 'next/dist/shared/lib/hooks-client-context.shared-runtime';
import { AuthProvider } from '@/hooks';
import { TooltipProvider } from '@/components/ui/tooltip';

const noop = () => {};
const router = {
  back: noop,
  forward: noop,
  refresh: noop,
  push: noop,
  replace: noop,
  prefetch: noop,
};

export interface NousProviderProps {
  children?: ReactNode;
  /**
   * Color scheme. The signed-in app defaults to dark (app/providers.tsx);
   * pass 'dark' for app-faithful screens. Light reads best on the white
   * design-pane cards, so it is the default here.
   */
  theme?: 'light' | 'dark';
  /** Path reported by usePathname() - e.g. '/chat' highlights that nav item. */
  pathname?: string;
}

export function NousProvider({ children, theme = 'light', pathname = '/' }: NousProviderProps) {
  // No retries and no refetch: there is no backend behind a design, so a
  // query settles into its error/empty state once instead of spinning.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false, staleTime: Infinity },
          mutations: { retry: false },
        },
      })
  );
  const [searchParams] = useState(() => new URLSearchParams());
  return (
    <ThemeProvider attribute="class" forcedTheme={theme} defaultTheme={theme}>
      <AppRouterContext.Provider value={router}>
        <PathnameContext.Provider value={pathname}>
          <SearchParamsContext.Provider value={searchParams}>
            <PathParamsContext.Provider value={{}}>
              <QueryClientProvider client={queryClient}>
                <AuthProvider>
                  <TooltipProvider delayDuration={200}>
                    {/* The theme surface the app gets from globals.css `body`
                        rules; hosts (preview cards, design canvases) paint
                        their own body, so the wrapper carries it. */}
                    <div className="bg-background text-foreground font-sans antialiased">
                      {children}
                    </div>
                  </TooltipProvider>
                </AuthProvider>
              </QueryClientProvider>
            </PathParamsContext.Provider>
          </SearchParamsContext.Provider>
        </PathnameContext.Provider>
      </AppRouterContext.Provider>
    </ThemeProvider>
  );
}
