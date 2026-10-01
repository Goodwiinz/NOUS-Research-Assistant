'use client';

/**
 * GOO-316 ORCID redirect target: forwards `code` and `state` (or ORCID's
 * `error`) to the authenticated API, which verifies the signed state for the
 * session user and keeps a non-secret receipt. Errors are the API's safe
 * codes, never ORCID's raw text.
 */

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import React, { useEffect, useRef, useState } from 'react';
import { projectService } from '@/services/projectService';

const MESSAGES: Record<string, string> = {
  orcid_denied: 'ORCID sign-in was cancelled.',
  orcid_state_invalid: 'This ORCID link expired or was not started by you.',
  orcid_exchange_failed: 'ORCID could not confirm the sign-in. Try again.',
};

function OrcidCallbackContent(): React.JSX.Element {
  const params = useSearchParams();
  const started = useRef(false);
  const [result, setResult] = useState<string>('Confirming your ORCID iD…');

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    projectService
      .completeOrcid({
        code: params.get('code'),
        state: params.get('state'),
        error: params.get('error'),
      })
      .then((receipt) =>
        setResult(
          `ORCID iD ${receipt.orcid} authenticated (${receipt.environment}).`
        )
      )
      .catch((error: unknown) => {
        const code = error instanceof Error ? error.message : '';
        setResult(MESSAGES[code] ?? 'ORCID sign-in failed.');
      });
  }, [params]);

  return (
    <main className="mx-auto max-w-md p-6 space-y-3 text-sm">
      <h1 className="text-base font-medium text-foreground">ORCID</h1>
      <p role="status" className="text-muted-foreground">
        {result}
      </p>
      <Link href="/research" className="text-primary underline">
        Back to research
      </Link>
    </main>
  );
}

export default function OrcidCallbackPage(): React.JSX.Element {
  return (
    <React.Suspense fallback={null}>
      <OrcidCallbackContent />
    </React.Suspense>
  );
}
