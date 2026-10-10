'use client';

import { useMemo } from 'react';
import { useRouter } from 'next/navigation';
import { confirmArtifactNavigation } from '@/utils/artifactNavigation';

/** Route controls in the chat layout share the editor's discard/stay decision. */
export function useArtifactRouter(): ReturnType<typeof useRouter> {
  const router = useRouter();
  return useMemo(
    () => ({
      ...router,
      push: (...args: Parameters<typeof router.push>) => {
        if (confirmArtifactNavigation()) router.push(...args);
      },
      replace: (...args: Parameters<typeof router.replace>) => {
        if (confirmArtifactNavigation()) router.replace(...args);
      },
    }),
    [router]
  );
}
