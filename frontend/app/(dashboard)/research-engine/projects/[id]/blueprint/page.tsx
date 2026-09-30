'use client';

import { useParams } from 'next/navigation';
import { LegacyProjectRedirect } from '@/components/research-engine/LegacyProjectRedirect';

export default function BlueprintPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  return (
    <div className="container mx-auto max-w-7xl p-6">
      <LegacyProjectRedirect engineProjectId={projectId} />
    </div>
  );
}
