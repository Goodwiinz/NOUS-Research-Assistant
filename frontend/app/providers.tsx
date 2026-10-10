'use client';

import '@/utils/artifactNavigation';

// Note: AnalyticsProvider is temporarily disabled for TypeScript strict mode
// import { AnalyticsProvider } from '@/components/analytics/AnalyticsProvider';
import { AuthProvider } from '@/hooks';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { setAppQueryClient } from '@/lib/query-client';
import { ThemeProvider } from 'next-themes';
import React, { useState } from 'react';
import { Toaster } from 'react-hot-toast';
import { StatsigClientProvider } from './statsig-provider';

interface ProvidersProps {
  children: React.ReactNode;
}

export function Providers({ children }: ProvidersProps) {
  const [queryClient] = useState(() => {
    const client = new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5 * 60 * 1000,
            gcTime: 10 * 60 * 1000,
            retry: 3,
            retryDelay: (attemptIndex) =>
              Math.min(1000 * 2 ** attemptIndex, 30000),
            refetchOnWindowFocus: false,
          },
          mutations: {
            // Mutations are non-idempotent (creates) — a retried mutation
            // duplicates the create. Fail fast, surface the error (R6-M15).
            retry: 0,
            retryDelay: 1000,
          },
        },
      });
    // Expose to non-React modules (Zustand stores) for cross-cache
    // invalidation — see src/lib/query-client.ts.
    setAppQueryClient(client);
    return client;
  });

  return (
    <ThemeProvider attribute="class" defaultTheme="dark" enableSystem>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <StatsigClientProvider>
            {children}
            <Toaster
              position="top-right"
              toastOptions={{
                duration: 4000,
                style: {
                  background: 'hsl(var(--card))',
                  color: 'hsl(var(--card-foreground))',
                  border: '1px solid hsl(var(--border))',
                },
              }}
            />
          </StatsigClientProvider>
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}

export default Providers;
