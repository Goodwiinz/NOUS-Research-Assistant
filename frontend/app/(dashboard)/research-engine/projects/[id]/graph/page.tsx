'use client';

import { use } from 'react';
import { LegacyProjectRedirect } from '@/components/research-engine/LegacyProjectRedirect';

interface GraphPageProps {
  params: Promise<{ id: string }>;
}

export default function GraphPage({ params }: GraphPageProps) {
  const { id } = use(params);

  return (
    <div className="container mx-auto max-w-7xl p-6">
      <LegacyProjectRedirect engineProjectId={id} />
    </div>
  );
}
