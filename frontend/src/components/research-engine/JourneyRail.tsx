'use client';

import { useEffect, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  CircleDot,
  Download,
  type LucideIcon,
} from 'lucide-react';
import {
  downloadAuditBundle,
  getJourney,
} from '@/services/researchEngineService';
import type {
  ApiJourneyStageKey,
  ApiJourneyStatus,
} from '@/types/api/research-journey-contract';

const LABELS: Record<ApiJourneyStageKey, string> = {
  plan: 'Plan',
  discover: 'Discover',
  select: 'Select',
  extract: 'Extract',
  write: 'Write',
};

const STATUS: Record<
  ApiJourneyStatus,
  { label: string; Icon: LucideIcon; tone: string }
> = {
  not_started: {
    label: 'Not started',
    Icon: Circle,
    tone: 'text-muted-foreground',
  },
  in_progress: { label: 'In progress', Icon: CircleDot, tone: 'text-primary' },
  attention: {
    label: 'Needs attention',
    Icon: AlertTriangle,
    tone: 'text-destructive',
  },
  complete: { label: 'Complete', Icon: CheckCircle2, tone: 'text-primary' },
};

export const journeyQueryKey = (projectId: string) =>
  ['project', projectId, 'research-engine', 'journey'] as const;

/** Numeric facts as short counts: "queued reports 4 · resolved reports 3". */
function counts(facts: Record<string, unknown>): string {
  return Object.entries(facts)
    .filter(([, value]) => typeof value === 'number')
    .map(([key, value]) => `${key.replace(/_/g, ' ')} ${String(value)}`)
    .join(' · ');
}

/**
 * GOO-308 Plan → Discover → Select → Extract → Write rail. The server derives
 * every status from persisted rows; the rail reports state and never gates
 * the panels below it, whose own server checks stay authoritative.
 */
export function JourneyRail({
  projectId,
}: {
  projectId: string;
}): ReactElement {
  const queryClient = useQueryClient();
  const journey = useQuery({
    queryKey: journeyQueryKey(projectId),
    queryFn: () => getJourney(projectId),
    retry: false,
  });
  const download = useMutation({
    mutationFn: () => downloadAuditBundle(projectId),
  });

  // ponytail: coarse; any mutation settling (isMutating back at 0)
  // refetches the rail. Add per-panel invalidation if the refetch is noisy.
  useEffect(
    () =>
      queryClient.getMutationCache().subscribe((event) => {
        const status = event.mutation?.state.status;
        if (
          event.type === 'updated' &&
          (status === 'success' || status === 'error') &&
          queryClient.isMutating() === 0
        ) {
          void queryClient.invalidateQueries({
            queryKey: journeyQueryKey(projectId),
          });
        }
      }),
    [projectId, queryClient]
  );

  const stages = journey.data?.stages ?? [];
  return (
    <section
      aria-labelledby={`journey-${projectId}`}
      className="rounded-xl border border-border bg-card p-5"
    >
      <h2 id={`journey-${projectId}`} className="font-medium text-foreground">
        Research journey
      </h2>
      {journey.isLoading && (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          Loading journey…
        </p>
      )}
      {journey.error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          Failed to load the research journey.
        </p>
      )}
      <ol
        aria-label="Research journey"
        className="mt-4 grid gap-3 sm:grid-cols-5"
      >
        {stages.map((stage) => {
          const { label, Icon, tone } = STATUS[stage.status];
          const current = journey.data?.current === stage.key;
          const facts = counts(stage.facts);
          return (
            <li
              key={stage.key}
              aria-current={current ? 'step' : undefined}
              className={`rounded-md border p-3 text-sm ${
                current ? 'border-primary' : 'border-border'
              }`}
            >
              <a
                href={`#journey-${stage.key}`}
                className="font-medium text-foreground hover:underline"
              >
                {LABELS[stage.key]}
              </a>
              <p className={`mt-1 flex items-center gap-1.5 ${tone}`}>
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {label}
              </p>
              {facts && (
                <p className="mt-1 text-xs text-muted-foreground">{facts}</p>
              )}
              {stage.blockers.length > 0 && (
                <ul className="mt-1 list-disc pl-4 text-xs text-muted-foreground">
                  {stage.blockers.map((blocker) => (
                    <li key={blocker}>{blocker}</li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={() => download.mutate()}
          disabled={download.isPending}
          className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm text-foreground disabled:opacity-50"
        >
          <Download className="h-4 w-4" aria-hidden="true" />
          Download audit bundle
        </button>
        {download.error && (
          <p role="alert" className="text-sm text-destructive">
            Failed to download the audit bundle.
          </p>
        )}
      </div>
    </section>
  );
}
