'use client';

import React, { useSyncExternalStore } from 'react';
import { useSearchParams } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { CliAuthApproval } from '@/components/auth/CliAuthApproval';
import {
  getAccountSessionRevision,
  onAccountSessionReset,
} from '@/lib/account-session';

function CliAuthPageSession(): React.JSX.Element {
  const { user, isAuthenticated, isLoading } = useAuth();
  const searchParams = useSearchParams();
  const sessionId = searchParams.get('session_id') ?? '';
  const accountRevision = useSyncExternalStore(
    onAccountSessionReset,
    getAccountSessionRevision,
    () => 0
  );
  // Typed consent and mutation observers belong to exactly this target/lifetime.
  // See CliAuthPage.test.tsx for mutation-verified session/account race cases.
  const consentKey = JSON.stringify([
    user?.id,
    isAuthenticated,
    accountRevision,
    sessionId,
  ]);
  return (
    <CliAuthApproval
      key={consentKey}
      sessionId={sessionId}
      linkCarriesCode={searchParams.has('code')}
      userId={user?.id ?? null}
      accountRevision={accountRevision}
      isAuthenticated={isAuthenticated}
      isLoading={isLoading}
    />
  );
}

export default function CliAuthPage(): React.JSX.Element {
  return (
    <React.Suspense fallback={null}>
      <CliAuthPageSession />
    </React.Suspense>
  );
}
