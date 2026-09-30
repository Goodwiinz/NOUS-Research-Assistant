'use client';

import { useEffect, useState, type ReactElement } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { Loader2 } from 'lucide-react';
import { listWorkflowLinkOptions } from '@/services/projectService';
import {
  getLegacyProject,
  linkProject,
} from '@/services/researchEngineService';

interface LegacyProjectRedirectProps {
  engineProjectId: string;
}

export function LegacyProjectRedirect({
  engineProjectId,
}: LegacyProjectRedirectProps): ReactElement {
  const router = useRouter();
  const [collectionId, setCollectionId] = useState('');
  const legacy = useQuery({
    queryKey: ['research-engine', 'legacy-project', engineProjectId],
    queryFn: () => getLegacyProject(engineProjectId),
    retry: false,
  });
  const collections = useQuery({
    queryKey: ['projects', 'research-engine-link-options'],
    queryFn: listWorkflowLinkOptions,
    enabled: legacy.isSuccess && !legacy.data.collection_id,
  });
  const link = useMutation({
    mutationFn: () => linkProject(engineProjectId, collectionId),
    onSuccess: (project) => {
      router.replace(`/projects/${project.project_id}?tab=workflow`);
    },
  });

  const canonicalProjectId =
    legacy.data?.project_id ?? legacy.data?.collection_id ?? null;
  useEffect(() => {
    if (canonicalProjectId) {
      router.replace(`/projects/${canonicalProjectId}?tab=workflow`);
    }
  }, [canonicalProjectId, router]);

  if (legacy.isLoading || canonicalProjectId) {
    return (
      <div role="status" className="flex items-center gap-2 p-6 text-sm">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Opening project workflow…
      </div>
    );
  }

  if (legacy.error) {
    return (
      <div role="alert" className="rounded-md border border-destructive p-4">
        This historical research project is unavailable or you do not have
        access.
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-xl rounded-xl border border-border bg-card p-6">
      <h1 className="text-xl font-semibold text-foreground">
        Link historical workflow
      </h1>
      <p className="mt-2 text-sm text-muted-foreground">
        {legacy.data?.name ?? 'This workflow'} predates canonical projects.
        Select an authorized project to preserve this deep link.
      </p>
      <label htmlFor="legacy-project-collection" className="mt-5 block text-sm">
        Project
      </label>
      <select
        id="legacy-project-collection"
        value={collectionId}
        onChange={(event) => setCollectionId(event.target.value)}
        className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
      >
        <option value="">Select a project</option>
        {(collections.data ?? []).map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => link.mutate()}
        disabled={!collectionId || link.isPending}
        className="mt-4 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
      >
        Link and open workflow
      </button>
      {link.error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          You must own the selected project or have administrator access.
        </p>
      )}
    </div>
  );
}
