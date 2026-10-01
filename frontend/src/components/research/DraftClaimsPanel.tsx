'use client';

/**
 * GOO-306: read-only list of the claims pinned to one draft version.
 * Creating, linking and assessing claims is GOO-307's UI.
 */

import type { ReactElement } from 'react';
import { Button } from '@/components/ui/button';
import { useBackendCapabilities } from '@/hooks/useBackendCapabilities';
import { useDraftClaims } from '@/hooks/useDraftClaims';
import { projectService } from '@/services/projectService';
import type {
  ApiClaimLinkKind,
  ApiClaimSummary,
} from '@/types/api/research-claims-contract';

const KIND_LABELS: Record<ApiClaimLinkKind, string> = {
  extraction: 'Extraction',
  source_span: 'Source span',
  legacy_unanchored: 'Legacy (unanchored)',
};
const PASSAGE_CHARS = 160;

// ponytail: users are shown by short id; resolve names when a members
// endpoint is wired into this step.
function person(userId: string | null | undefined): string {
  return userId ? userId.slice(0, 8) : 'unknown';
}

function words(value: string): string {
  return value.replace(/_/g, ' ');
}

function Badge({ label }: { label: string }): ReactElement {
  return (
    <span
      aria-label={label}
      className="rounded border border-border bg-muted px-1.5 py-0.5 text-xs"
    >
      {label}
    </span>
  );
}

function ClaimRow({ claim }: { claim: ApiClaimSummary }): ReactElement {
  const { version, links, assessment } = claim;
  const passage =
    version.text.length > PASSAGE_CHARS
      ? `${version.text.slice(0, PASSAGE_CHARS)}…`
      : version.text;
  const counts = new Map<ApiClaimLinkKind, number>();
  for (const link of links) {
    counts.set(link.kind, (counts.get(link.kind) ?? 0) + 1);
  }
  const observed = links
    .map((link) => link.latest_observation)
    .filter((o): o is NonNullable<typeof o> => Boolean(o))
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const latest = observed[observed.length - 1];
  return (
    <li className="space-y-1 border-b border-border py-2 last:border-b-0">
      <p className="text-sm">
        <span className="mr-2 font-mono text-xs text-muted-foreground">
          v{version.version_no}
        </span>
        {passage}
      </p>
      <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
        {version.kind === 'interpretation' && (
          <Badge
            label={`Interpretation — ${person(version.attributed_to_user_id)}`}
          />
        )}
        {[...counts].map(([kind, count]) =>
          kind === 'legacy_unanchored' ? (
            <Badge key={kind} label={`${KIND_LABELS[kind]} ×${count}`} />
          ) : (
            <span key={kind}>
              {KIND_LABELS[kind]}: {count}
            </span>
          )
        )}
        {links.some((link) => link.source_changed) && (
          <Badge label="Source changed" />
        )}
        {latest && <span>Model stance: {words(latest.stance)}</span>}
        {assessment ? (
          <Badge
            label={`Accepted: ${words(assessment.stance)} by ${person(
              assessment.assessed_by_id
            )}`}
          />
        ) : (
          <Badge label="Unassessed" />
        )}
      </div>
    </li>
  );
}

export function DraftClaimsPanel({
  projectId,
  draftId,
}: {
  projectId: string;
  draftId: string;
}): ReactElement | null {
  const capabilities = useBackendCapabilities();
  const claims = useDraftClaims(projectId, draftId);
  if (!capabilities.draftClaims || !claims.data) return null;
  const { items, counts } = claims.data;
  return (
    <section
      aria-label="Claims"
      className="rounded-md border border-border p-3 text-sm"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-medium">
          Claims ({counts.claims}; {counts.assessed} assessed)
        </h3>
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            void projectService.downloadClaimsExport(projectId, draftId)
          }
        >
          Export evidence
        </Button>
      </div>
      {items.length === 0 ? (
        <p className="text-muted-foreground">No claims on this draft yet.</p>
      ) : (
        <ul>
          {items.map((claim) => (
            <ClaimRow key={claim.claim_id} claim={claim} />
          ))}
        </ul>
      )}
    </section>
  );
}
