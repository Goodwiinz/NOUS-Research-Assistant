'use client';

import { useParams } from 'next/navigation';
import type { ReactElement } from 'react';

import { IntegrationActionApproval } from '@/components/integrations/IntegrationActionApproval';

export default function IntegrationActionPage(): ReactElement {
  const params = useParams<{ invocationId: string }>();
  return <IntegrationActionApproval invocationId={params.invocationId ?? ''} />;
}
