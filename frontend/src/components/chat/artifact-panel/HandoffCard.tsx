'use client';

import type { ReactElement } from 'react';

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
          <li key={index}>{item}</li>
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
      <p className="mt-1 text-sm text-(--nous-fg-1)">{handoff.goal}</p>
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
                <li key={result.artifact_version_id}>
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
}: {
  threadId: string | null | undefined;
}): ReactElement | null {
  const { data: handoff } = useThreadHandoff(threadId);
  const { data: artifacts } = useThreadArtifacts(handoff ? threadId : null);
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);
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
