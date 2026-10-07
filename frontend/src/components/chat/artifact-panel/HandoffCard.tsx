'use client';

import type { ReactElement } from 'react';
import { ScrollText } from 'lucide-react';

import { useThreadArtifacts } from '@/hooks/chat/useThreadArtifacts';
import { useThreadHandoff } from '@/hooks/chat/useThreadHandoff';
import type { ThreadArtifact } from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import type { ApiHandoff } from '@/types/api/integration-handoff-contract';

function formatWhen(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function Section({
  label,
  items,
}: {
  label: string;
  items: string[];
}): ReactElement | null {
  if (items.length === 0) return null;
  return (
    <div className="mt-2">
      <h4 className="font-nous-mono text-[10px] uppercase tracking-wider text-(--nous-fg-3)">
        {label}
      </h4>
      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-(--nous-fg-2)">
        {items.map((item, index) => (
          <li key={index} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Harness-written handoff for this chat: what the last session set out to do,
 * what it decided, which published versions it produced, and what remains.
 * Renders nothing when there is no handoff.
 */
export function HandoffCard({
  handoff,
  artifacts = [],
  onOpenVersion,
}: {
  handoff: ApiHandoff | null | undefined;
  /** Thread artifacts, used to resolve result versions into openable files. */
  artifacts?: ThreadArtifact[];
  onOpenVersion?: (artifact: ThreadArtifact) => void;
}): ReactElement | null {
  if (!handoff) return null;
  const byVersion = new Map(artifacts.map((a) => [a.version.versionId, a]));
  return (
    <section
      aria-label="Session handoff"
      className="m-3 rounded-xl border border-(--nous-border-1) bg-(--nous-bg-2) p-3"
    >
      <h3 className="font-nous-ui text-xs font-semibold text-(--nous-fg-1)">
        Handoff
      </h3>
      <p className="mt-1 text-sm break-words text-(--nous-fg-1)">
        {handoff.goal}
      </p>
      <Section label="Decisions" items={handoff.decisions} />
      {handoff.results.length > 0 && (
        <div className="mt-2">
          <h4 className="font-nous-mono text-[10px] uppercase tracking-wider text-(--nous-fg-3)">
            Results
          </h4>
          <ul className="mt-1 space-y-0.5 text-xs text-(--nous-fg-2)">
            {handoff.results.map((result) => {
              const artifact = byVersion.get(result.artifact_version_id);
              return (
                <li key={result.artifact_version_id} className="break-words">
                  {artifact && onOpenVersion ? (
                    <button
                      type="button"
                      onClick={() => onOpenVersion(artifact)}
                      className="text-left text-(--nous-fg-accent-safe) underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {artifact.version.title}
                    </button>
                  ) : (
                    <span className="font-nous-mono">
                      {result.artifact_version_id.slice(0, 8)}
                    </span>
                  )}
                  {' — '}
                  {result.summary}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <Section label="Remaining" items={handoff.remaining} />
      <p className="font-nous-mono mt-3 text-[10px] text-(--nous-fg-3)">
        v{handoff.version} · {handoff.harness_name} ·{' '}
        {formatWhen(handoff.created_at)}
      </p>
    </section>
  );
}

/** The handoff for the open chat, wired to the panel's artifact viewer. */
export function ThreadHandoffCard({
  threadId,
  showEmpty = false,
}: {
  threadId: string | null | undefined;
  /** In the handoff-only view, say so instead of rendering nothing. */
  showEmpty?: boolean;
}): ReactElement | null {
  const { data: handoff, isError, refetch } = useThreadHandoff(threadId);
  const { data: artifacts } = useThreadArtifacts(handoff ? threadId : null);
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);
  if (isError) {
    // A stable notice, never the raw error: a blank would read as "no handoff".
    return (
      <div
        role="alert"
        className="m-3 flex items-center gap-2 rounded-xl border border-(--nous-border-1) bg-(--nous-bg-2) p-3 text-xs text-(--nous-fg-2)"
      >
        <span>Couldn&apos;t load the chat handoff.</span>
        <button
          type="button"
          onClick={() => void refetch()}
          className="rounded-md border border-(--nous-border-1) px-2 py-1 text-(--nous-fg-1) hover:bg-(--nous-aurum) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        >
          Retry
        </button>
      </div>
    );
  }
  if (!handoff && showEmpty && handoff !== undefined)
    return (
      <p className="m-3 text-xs text-(--nous-fg-3)">
        No handoff in this chat yet.
      </p>
    );
  return (
    <HandoffCard
      handoff={handoff}
      artifacts={artifacts}
      onOpenVersion={(item) =>
        openArtifact(
          {
            kind: 'generated',
            artifactId: item.version.artifactId,
            versionId: item.version.versionId,
            title: item.version.title,
          },
          { source: 'user' }
        )
      }
    />
  );
}

/**
 * Header entry point: reachable without an open artifact. Shown only when the
 * chat has a handoff and the panel is not already showing one.
 */
export function HandoffEntryButton({
  threadId,
}: {
  threadId: string | null | undefined;
}): ReactElement | null {
  const { data: handoff } = useThreadHandoff(threadId);
  const panelOpen = useArtifactPanelStore(
    (s) => s.isOpen && s.artifact !== null
  );
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);
  if (!handoff || panelOpen) return null;
  return (
    <button
      type="button"
      onClick={() =>
        openArtifact({ kind: 'handoff', title: 'Handoff' }, { source: 'user' })
      }
      className="font-nous-mono inline-flex h-8 items-center gap-1 rounded-lg px-2 text-[11px] text-(--nous-fg-2) transition-colors hover:bg-(--nous-sol)/8 hover:text-(--nous-fg-1) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ScrollText aria-hidden="true" className="h-3.5 w-3.5" />
      Handoff v{handoff.version}
    </button>
  );
}
