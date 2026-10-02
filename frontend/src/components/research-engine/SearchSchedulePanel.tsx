'use client';

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import {
  createSearchSchedule,
  exportSearchDelta,
  getSearchDelta,
  listSearchSchedules,
  versionSearchSchedule,
  type ProjectRoleAssignment,
  type SearchDeltaItem,
  type SearchSchedule,
} from '@/services/researchEngineService';

interface SearchSchedulePanelProps {
  projectId: string;
  roles: ProjectRoleAssignment[];
  readOnly?: boolean;
}

/** Why a work is ``unknown``: never a deletion or a retraction. */
const REASON_LABEL: Record<string, string> = {
  provider_failed: 'A provider that could return it failed',
  provider_capped: 'A provider hit its result cap',
  not_returned: 'Not returned by this search',
  no_doi_publication_check_not_performed:
    'No DOI, so no publication check was performed',
  merge_unresolved: 'Identity changed since the baseline (unresolved)',
};
const CLASS_LABEL: Record<string, string> = {
  new: 'New',
  changed: 'Changed',
  corrected_retracted: 'Corrected or retracted',
  unchanged: 'Unchanged',
  unknown: 'Unknown',
};
const CLASSES = Object.keys(CLASS_LABEL);
const STATUS_LABEL: Record<string, string> = {
  disabled: 'Disabled',
  scheduled: 'Scheduled',
  running: 'Running',
  blocked: 'Blocked',
  failed: 'Failed',
  ok: 'Up to date',
};
const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50';
const INPUT =
  'rounded-md border border-border bg-background px-2 py-1.5 text-sm';
const intl: typeof Intl & {
  supportedValuesOf?: (key: 'timeZone') => string[];
} = Intl;
const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const ZONES = intl.supportedValuesOf?.('timeZone') ?? [LOCAL_ZONE];

/** ``2026-10-05T06:00`` in its schedule's zone, as written there. */
function localFire(value: string, zone: string): string {
  return `${value.replace('T', ' ')} ${zone}`;
}

function DeltaTable({ items }: { items: SearchDeltaItem[] }): ReactElement {
  return (
    <table className="mt-3 w-full text-left text-sm">
      <thead className="text-xs text-muted-foreground">
        <tr>
          <th className="py-1 pr-3 font-medium">Report</th>
          <th className="py-1 pr-3 font-medium">Class</th>
          <th className="py-1 font-medium">Evidence</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border">
        {items.map((item) => {
          const notices = (item.evidence.notices ?? []) as {
            notice_doi: string;
            type: string;
          }[];
          return (
            <tr key={item.report_id}>
              <td className="py-1 pr-3 font-mono text-xs">{item.report_id}</td>
              <td className="py-1 pr-3">{CLASS_LABEL[item.class]}</td>
              <td className="py-1 text-muted-foreground">
                {item.class === 'unknown' && item.reason
                  ? REASON_LABEL[item.reason]
                  : null}
                {item.class === 'corrected_retracted' &&
                  notices.map((notice) => (
                    <a
                      key={`${notice.notice_doi}-${notice.type}`}
                      href={`https://doi.org/${notice.notice_doi}`}
                      target="_blank"
                      rel="noreferrer"
                      className="mr-2 underline"
                    >
                      {notice.type.replace(/_/g, ' ')}: {notice.notice_doi}
                    </a>
                  ))}
                {item.class === 'changed' &&
                  `Changed: ${Object.keys(
                    (item.evidence.fields ?? {}) as Record<string, unknown>
                  ).join(', ')}`}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ScheduleRow({
  projectId,
  schedule,
  canSupervise,
}: {
  projectId: string;
  schedule: SearchSchedule;
  canSupervise: boolean;
}): ReactElement {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const last = schedule.last_execution;
  const succeeded = last?.status === 'succeeded' ? last : null;
  const delta = useQuery({
    queryKey: ['project', projectId, 'search-delta', succeeded?.id],
    queryFn: () => getSearchDelta(projectId, succeeded?.id ?? ''),
    enabled: open && Boolean(succeeded),
  });
  const toggle = useMutation({
    mutationFn: () =>
      versionSearchSchedule(projectId, schedule.schedule_id, {
        expected_tip_id: schedule.tip.id,
        enabled: !schedule.tip.enabled,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () =>
      void queryClient.invalidateQueries({
        queryKey: ['project', projectId, 'search-schedules'],
      }),
  });
  const tip = schedule.tip;
  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-foreground">{tip.query}</span>
        <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
          {STATUS_LABEL[schedule.status]}
        </span>
        <span className="text-muted-foreground">
          {tip.cron} ({tip.timezone})
        </span>
        {canSupervise && (
          <button
            type="button"
            className={`${BUTTON} ml-auto`}
            onClick={() => toggle.mutate()}
            disabled={toggle.isPending}
          >
            {tip.enabled ? 'Disable' : 'Enable'}
          </button>
        )}
      </div>
      {schedule.next_fire_local && (
        <p className="text-sm text-muted-foreground">
          Next run: {localFire(schedule.next_fire_local, tip.timezone)}
        </p>
      )}
      {last ? (
        <div className="text-sm text-muted-foreground">
          Last run {localFire(last.scheduled_local, tip.timezone)}:{' '}
          {last.status}
          {last.missed_fires > 0 &&
            ` (${last.missed_fires} missed fires coalesced)`}
          {last.status === 'failed' &&
            ` — ${last.attempts[last.attempts.length - 1]?.reason ?? ''}`}
          {last.counts && (
            <ul className="mt-1 flex flex-wrap gap-3" aria-label="Delta counts">
              {CLASSES.map((name) => (
                <li key={name}>
                  {CLASS_LABEL[name]}:{' '}
                  {last.counts?.[name as keyof typeof last.counts] ?? 0}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Not run yet.</p>
      )}
      {succeeded && (
        <div className="flex gap-2">
          <button
            type="button"
            className={BUTTON}
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            {open ? 'Hide delta' : 'View delta'}
          </button>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void exportSearchDelta(projectId, succeeded.id)}
          >
            Export delta
          </button>
        </div>
      )}
      {open && delta.isLoading && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading delta…
        </p>
      )}
      {open && delta.data && <DeltaTable items={delta.data.body.items} />}
      {(toggle.error || delta.error) && (
        <p role="alert" className="text-sm text-destructive">
          {toggle.error instanceof Error
            ? toggle.error.message
            : 'Failed to load the delta.'}
        </p>
      )}
    </li>
  );
}

/**
 * GOO-319: scheduled search updates in the Workflow tab. A supervisor pins
 * one completed run's search strategy to a cron schedule in a timezone; each
 * run's delta classifies every work, and a missing work is ``unknown`` with
 * its reason, never deleted or retracted.
 */
export function SearchSchedulePanel({
  projectId,
  roles,
  readOnly = false,
}: SearchSchedulePanelProps): ReactElement {
  const queryClient = useQueryClient();
  const userId = useAuth().user?.id;
  const canSupervise =
    !readOnly &&
    roles.some((r) => r.user_id === userId && r.role === 'supervisor');
  const queryKey = ['project', projectId, 'search-schedules'] as const;
  const listing = useQuery({
    queryKey,
    queryFn: () => listSearchSchedules(projectId),
    retry: false,
  });
  const [strategy, setStrategy] = useState('');
  const [cron, setCron] = useState('0 6 * * 1');
  const [zone, setZone] = useState(LOCAL_ZONE);
  const options = listing.data?.strategies ?? [];
  const picked = options.find((o) => o.strategy_version === strategy);
  const create = useMutation({
    mutationFn: () =>
      createSearchSchedule(projectId, {
        source_run_id: picked?.source_run_id ?? '',
        step_id: picked?.step_id ?? '',
        strategy_version: strategy,
        cron: cron.trim(),
        timezone: zone,
        enabled: true,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey }),
  });

  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <h3 className="font-medium text-foreground">Scheduled search updates</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        Reruns a saved search on a schedule and classifies what changed against
        the previous corpus snapshot.
      </p>
      {listing.isLoading && (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          Loading schedules…
        </p>
      )}
      {listing.error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          Failed to load schedules.
        </p>
      )}
      {listing.data && listing.data.schedules.length === 0 && (
        <p className="mt-3 text-sm text-muted-foreground">No schedules yet.</p>
      )}
      <ul className="mt-2 divide-y divide-border">
        {(listing.data?.schedules ?? []).map((schedule) => (
          <ScheduleRow
            key={schedule.schedule_id}
            projectId={projectId}
            schedule={schedule}
            canSupervise={canSupervise}
          />
        ))}
      </ul>
      {canSupervise && (
        <form
          className="mt-4 grid gap-2 sm:grid-cols-4"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <label className="text-sm sm:col-span-2">
            <span className="block text-muted-foreground">Search strategy</span>
            <select
              className={`${INPUT} w-full`}
              value={strategy}
              onChange={(event) => setStrategy(event.target.value)}
            >
              <option value="">Choose a completed run&apos;s search</option>
              {options.map((option) => (
                <option
                  key={`${option.source_run_id}-${option.strategy_version}`}
                  value={option.strategy_version}
                  disabled={!option.current_protocol}
                >
                  {option.query} ({option.providers.join(', ')})
                  {option.current_protocol ? '' : ' — superseded protocol'}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">
              Cron (hourly at most)
            </span>
            <input
              className={`${INPUT} w-full font-mono`}
              value={cron}
              onChange={(event) => setCron(event.target.value)}
            />
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">Timezone</span>
            <select
              className={`${INPUT} w-full`}
              value={zone}
              onChange={(event) => setZone(event.target.value)}
            >
              {ZONES.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 sm:col-span-4 sm:justify-self-start"
            disabled={!picked || !cron.trim() || create.isPending}
          >
            Save schedule
          </button>
          {create.error && (
            <p role="alert" className="text-sm text-destructive sm:col-span-4">
              {create.error instanceof Error
                ? create.error.message
                : 'Failed to save the schedule.'}
            </p>
          )}
        </form>
      )}
    </section>
  );
}
