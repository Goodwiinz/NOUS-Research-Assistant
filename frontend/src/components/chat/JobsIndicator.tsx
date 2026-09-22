'use client';

import React from 'react';
import { Loader2, AlertCircle, Ban, Check, Activity } from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import {
  useProcessingJobs,
  isJobActive,
  isJobCancelled,
  isJobFailed,
  jobStatusLabel,
} from '@/hooks/chat/useProcessingJobs';
import type { ProcessingJobStatus } from '@/services/entityService';

/** "Ingesting papers" reads better than "arxiv_ingest". */
function jobTitle(job: ProcessingJobStatus): string {
  const label = job.job_type.replace(/[_-]+/g, ' ').trim();
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function elapsed(job: ProcessingJobStatus): string | null {
  const seconds =
    job.duration_seconds ??
    (job.started_at
      ? (Date.now() - new Date(job.started_at).getTime()) / 1000
      : null);
  if (seconds === null || Number.isNaN(seconds) || seconds < 0) return null;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function JobRow({ job }: { job: ProcessingJobStatus }): React.ReactElement {
  const active = isJobActive(job);
  const failed = isJobFailed(job);
  const cancelled = isJobCancelled(job);
  const status = jobStatusLabel(job);
  const time = elapsed(job);
  // A running job with no reported percentage still gets a bar, held at a
  // sliver — a missing bar reads as "not started", which is worse than vague.
  const percent = Math.max(0, Math.min(100, job.progress_percentage ?? 0));

  return (
    <li className="px-3 py-2.5">
      <div className="flex items-center gap-2">
        {failed ? (
          <AlertCircle
            className="h-3.5 w-3.5 shrink-0"
            style={{ color: 'hsl(var(--destructive))' }}
            aria-hidden
          />
        ) : cancelled ? (
          // Terminal but not a success: a tick here told the reader their
          // cancelled ingest had finished.
          <Ban
            className="h-3.5 w-3.5 shrink-0 text-(--nous-fg-3)"
            aria-hidden
          />
        ) : active ? (
          <Loader2
            className="h-3.5 w-3.5 shrink-0 animate-spin text-(--nous-sol) motion-reduce:animate-none"
            aria-hidden
          />
        ) : (
          <Check
            className="h-3.5 w-3.5 shrink-0 text-(--nous-terra)"
            aria-hidden
          />
        )}

        <span
          className={cn(
            'min-w-0 flex-1 truncate font-nous-ui text-[12px] text-(--nous-fg-1)',
            cancelled && 'line-through text-(--nous-fg-3)'
          )}
        >
          {jobTitle(job)}
        </span>

        {/* The icon is the only visual status marker and it is decorative, so
            the state has to reach assistive tech as text. */}
        <span className="sr-only">{status}</span>

        {time && (
          <span className="shrink-0 font-nous-mono text-[10px] tabular-nums text-(--nous-fg-3)">
            {time}
          </span>
        )}
      </div>

      {(job.current_step || (active && percent > 0)) && (
        <p className="mt-1 truncate pl-5.5 font-nous-ui text-[11px] text-(--nous-fg-3)">
          {job.current_step ?? `${Math.round(percent)}%`}
        </p>
      )}

      {active && (
        <span
          className="mt-1.5 ml-5.5 block h-[3px] overflow-hidden rounded-sm"
          style={{ background: 'var(--nous-border-1)' }}
          role="progressbar"
          aria-valuenow={Math.round(percent)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${jobTitle(job)} progress`}
        >
          <span
            className="block h-full w-full origin-left rounded-sm transition-transform duration-500"
            style={{
              transform: `scaleX(${Math.max(percent, 4) / 100})`,
              background: 'var(--nous-sol)',
            }}
          />
        </span>
      )}

      {failed && job.error_message && (
        <p className="mt-1 pl-5.5 font-nous-ui text-[11px] text-(--nous-fg-2)">
          {job.error_message}
        </p>
      )}
    </li>
  );
}

/**
 * Header entry point for background work. Uploads, ingests and extractions run
 * on Celery and report through `/processing/jobs`, but the chat surface has
 * never shown them — a long ingest was indistinguishable from one that failed
 * ten minutes ago.
 */
export function JobsIndicator(): React.ReactElement | null {
  const { jobs, activeCount, isInitialLoading, error } = useProcessingJobs();

  // Nothing has ever run: don't put a dead control in the header.
  if (isInitialLoading || (jobs.length === 0 && !error)) return null;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={
            activeCount > 0
              ? `Background jobs: ${activeCount} running`
              : 'Background jobs'
          }
          className={cn(
            'relative inline-flex items-center gap-1.5 rounded-lg p-1.5 transition-colors',
            'text-(--nous-fg-3) hover:bg-(--nous-sol)/8 hover:text-(--nous-fg-1)',
            'focus:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40'
          )}
        >
          <Activity className="h-4 w-4" aria-hidden />
          {activeCount > 0 && (
            <span className="font-nous-mono text-[10px] tabular-nums text-(--nous-sol)">
              {activeCount}
            </span>
          )}
        </button>
      </PopoverTrigger>

      <PopoverContent
        align="end"
        sideOffset={8}
        className="w-[320px] overflow-hidden border-(--nous-border-1) bg-(--nous-bg-1) p-0"
      >
        <div className="border-b border-(--nous-border-1) bg-(--nous-bg-2) px-3 py-2">
          <p className="font-nous-ui text-[11px] font-medium text-(--nous-fg-2)">
            {activeCount > 0 ? `${activeCount} running` : 'No jobs running'}
          </p>
        </div>

        {error && (
          <p className="border-b border-(--nous-border-1) px-3 py-2 font-nous-ui text-[11px] text-(--nous-fg-3)">
            Showing the last known state. {error}
          </p>
        )}

        <ul className="max-h-[320px] divide-y divide-(--nous-border-1) overflow-y-auto">
          {jobs.map((job) => (
            <JobRow key={job.id} job={job} />
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

export default JobsIndicator;
