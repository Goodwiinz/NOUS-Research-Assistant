'use client';

import { useParams } from 'next/navigation';
import type { ReactElement } from 'react';
import { SharedArtifactView } from '@/components/chat/artifact-panel/SharedArtifactView';

export default function SharedArtifactPage(): ReactElement {
  const params = useParams<{ shareId: string }>();
  return <SharedArtifactView shareId={params.shareId ?? ''} />;
}
