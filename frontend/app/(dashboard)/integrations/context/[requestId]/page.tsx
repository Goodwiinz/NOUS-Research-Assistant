'use client';

import { useParams } from 'next/navigation';
import type { ReactElement } from 'react';

import { ContextSelection } from '@/components/integrations/ContextSelection';

export default function IntegrationContextPage(): ReactElement {
  const params = useParams<{ requestId: string }>();
  return <ContextSelection requestId={params.requestId ?? ''} />;
}
