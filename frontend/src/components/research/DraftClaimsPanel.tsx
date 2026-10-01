'use client';

/**
 * GOO-306: the claims pinned to one draft version. Read-only unless the
 * caller passes ``canEdit`` and the draft ``content`` (GOO-308): then a
 * passage can be claimed, linked to an accepted extraction value and its
 * stance observed; ``roles`` including ``adjudicator`` adds the assessment
 * form. Every POST carries one idempotency key per form open, and a 409
 * refetches the claims and shows the server's detail.
 */

import { useState, type ReactElement } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { useBackendCapabilities } from '@/hooks/useBackendCapabilities';
import { draftClaimsQueryKey, useDraftClaims } from '@/hooks/useDraftClaims';
import { projectService } from '@/services/projectService';
import {
  listFigures,
  listSynthesis,
  type Figure,
  type ResearchProjectRole,
  type SynthesisResult,
} from '@/services/researchEngineService';
import { FigureLineage } from '@/components/research-engine/FigureLineage';
import {
  getCellObservations,
  getMatrix,
  listMatrices,
} from '@/services/scispaceService';
import { APIErrorClass } from '@/types/api';
import type {
  ApiClaimAssessmentStance,
  ApiClaimLink,
  ApiClaimLinkKind,
  ApiClaimSummary,
} from '@/types/api/research-claims-contract';

const KIND_LABELS: Record<ApiClaimLinkKind, string> = {
  extraction: 'Extraction',
  source_span: 'Source span',
  legacy_unanchored: 'Legacy (unanchored)',
  synthesis_result: 'Synthesis result',
  figure: 'Figure',
};
// Links that cite a computed artifact carry no model stance (GOO-311/312).
const NO_STANCE: ApiClaimLinkKind[] = ['synthesis_result', 'figure'];
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

/** GOO-312: a figure link opens its output -> run -> data -> protocol chain. */
function FigureLink({
  projectId,
  figureId,
}: {
  projectId: string;
  figureId: string;
}): ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <div className="text-xs">
      <Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
        {open ? 'Hide' : 'Show'} figure lineage {figureId.slice(0, 8)}
      </Button>
      {open && <FigureLineage projectId={projectId} figureId={figureId} />}
    </div>
  );
}

function ClaimRow({
  claim,
  projectId,
  children,
}: {
  claim: ApiClaimSummary;
  projectId: string;
  children?: ReactElement | false;
}): ReactElement {
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
      {links
        .filter((link) => link.kind === 'figure' && link.figure_id)
        .map((link) => (
          <FigureLink
            key={link.id}
            projectId={projectId}
            figureId={link.figure_id as string}
          />
        ))}
      {children}
    </li>
  );
}

const STANCES: ApiClaimAssessmentStance[] = [
  'supporting',
  'opposing',
  'neutral',
  'not_addressed',
  'unresolved',
];

const newKey = (): string => crypto.randomUUID();

/** A UTF-16 offset (what ``selectionStart`` reports) as a code-point offset
 * (what the server's ``start_char``/``end_char`` mean). */
export function codePoints(text: string, utf16: number): number {
  return Array.from(text.slice(0, utf16)).length;
}

interface LinkTarget {
  matrixId: string;
  documentId: string;
  fieldId: string;
  label: string;
}

/** A write that refreshes the claims; a 409 refetches them too. */
function useClaimWrite<V>(
  projectId: string,
  draftId: string,
  mutationFn: (value: V) => Promise<unknown>,
  onSuccess: () => void
): UseMutationResult<unknown, Error, V> {
  const queryClient = useQueryClient();
  const refresh = (): void =>
    void queryClient.invalidateQueries({
      queryKey: draftClaimsQueryKey(projectId, draftId),
    });
  return useMutation({
    mutationFn,
    onSuccess: () => {
      onSuccess();
      refresh();
    },
    onError: (error) => {
      if (error instanceof APIErrorClass && error.error.status_code === 409) {
        refresh();
      }
    },
  });
}

function WriteError({ error }: { error: Error | null }): ReactElement | null {
  return error ? (
    <p role="alert" className="text-xs text-destructive">
      {error.message || 'Request failed'}
    </p>
  ) : null;
}

function ClaimAuthoring({
  projectId,
  draftId,
  content,
}: {
  projectId: string;
  draftId: string;
  content: string;
}): ReactElement {
  const [key, setKey] = useState(newKey);
  const [range, setRange] = useState<[number, number]>([0, 0]);
  const [kind, setKind] = useState<'factual' | 'interpretation'>('factual');
  const create = useClaimWrite(
    projectId,
    draftId,
    ([start, end]: [number, number]) =>
      projectService.createClaim(projectId, {
        draft_id: draftId,
        start_char: codePoints(content, start),
        end_char: codePoints(content, end),
        text: content.slice(start, end),
        kind,
        idempotency_key: key,
      }),
    () => setKey(newKey())
  );
  return (
    <div className="space-y-2">
      <label htmlFor={`claim-source-${draftId}`} className="text-xs">
        Select a passage to claim
      </label>
      <textarea
        id={`claim-source-${draftId}`}
        readOnly
        value={content}
        rows={6}
        onSelect={(event) =>
          setRange([
            event.currentTarget.selectionStart,
            event.currentTarget.selectionEnd,
          ])
        }
        className="w-full rounded-md border border-border bg-background p-2 font-mono text-xs"
      />
      <div className="flex items-center gap-2">
        <label className="sr-only" htmlFor={`claim-kind-${draftId}`}>
          Claim kind
        </label>
        <select
          id={`claim-kind-${draftId}`}
          value={kind}
          onChange={(event) =>
            setKind(event.target.value as 'factual' | 'interpretation')
          }
          className="rounded-md border border-border bg-background px-2 py-1 text-xs"
        >
          <option value="factual">factual</option>
          <option value="interpretation">interpretation</option>
        </select>
        <Button
          size="sm"
          disabled={range[0] === range[1] || create.isPending}
          onClick={() => create.mutate(range)}
        >
          Create claim
        </Button>
      </div>
      <WriteError error={create.error} />
    </div>
  );
}

function liveLinks(links: ApiClaimLink[]): ApiClaimLink[] {
  return links.filter(
    (link) =>
      link.status === 'linked' &&
      !links.some((other) => other.supersedes_link_id === link.id)
  );
}

function ClaimControls({
  projectId,
  draftId,
  claim,
  targets,
  results,
  figures,
  adjudicator,
}: {
  projectId: string;
  draftId: string;
  claim: ApiClaimSummary;
  targets: LinkTarget[];
  /** GOO-311: current computed synthesis results a claim may cite. */
  results: SynthesisResult[];
  /** GOO-312: current figure tips a claim may cite. */
  figures: Figure[];
  adjudicator: boolean;
}): ReactElement {
  const [linkKey, setLinkKey] = useState(newKey);
  const [observeKey, setObserveKey] = useState(newKey);
  const [assessKey, setAssessKey] = useState(newKey);
  const [target, setTarget] = useState('');
  const [resultId, setResultId] = useState('');
  const [figureId, setFigureId] = useState('');
  const [stance, setStance] = useState<ApiClaimAssessmentStance>('supporting');
  const [chosen, setChosen] = useState<string[]>([]);
  const [rationale, setRationale] = useState('');
  const live = liveLinks(claim.links);
  // ponytail: source_span links stay API-only; the journey's Extract→Write
  // path runs through accepted values.
  const link = useClaimWrite(
    projectId,
    draftId,
    async (picked: LinkTarget) => {
      const { accepted_chain: chain } = await getCellObservations(
        picked.matrixId,
        picked.documentId,
        picked.fieldId
      );
      const tip = chain.find(
        (a) => !chain.some((b) => b.supersedes_accepted_value_id === a.id)
      );
      if (!tip) throw new Error('This cell has no accepted value');
      return projectService.linkClaimEvidence(projectId, claim.claim_id, {
        claim_version_id: claim.version.id,
        kind: 'extraction',
        accepted_value_id: tip.id,
        status: 'linked',
        idempotency_key: linkKey,
      });
    },
    () => setLinkKey(newKey())
  );
  const linkResult = useClaimWrite(
    projectId,
    draftId,
    (synthesisResultId: string) =>
      projectService.linkClaimEvidence(projectId, claim.claim_id, {
        claim_version_id: claim.version.id,
        kind: 'synthesis_result',
        synthesis_result_id: synthesisResultId,
        status: 'linked',
        idempotency_key: linkKey,
      }),
    () => {
      setLinkKey(newKey());
      setResultId('');
    }
  );
  const linkFigure = useClaimWrite(
    projectId,
    draftId,
    (picked: string) =>
      projectService.linkClaimEvidence(projectId, claim.claim_id, {
        claim_version_id: claim.version.id,
        kind: 'figure',
        figure_id: picked,
        status: 'linked',
        idempotency_key: linkKey,
      }),
    () => {
      setLinkKey(newKey());
      setFigureId('');
    }
  );
  const observe = useClaimWrite(
    projectId,
    draftId,
    (linkId: string) =>
      projectService.observeClaimLink(
        projectId,
        claim.claim_id,
        linkId,
        observeKey
      ),
    () => setObserveKey(newKey())
  );
  const assess = useClaimWrite(
    projectId,
    draftId,
    () =>
      projectService.assessClaim(projectId, claim.claim_id, {
        claim_version_id: claim.version.id,
        stance,
        link_ids: chosen,
        rationale,
        supersedes_assessment_id: claim.assessment?.id ?? null,
        idempotency_key: assessKey,
      }),
    () => {
      setAssessKey(newKey());
      setRationale('');
    }
  );
  const picked = targets.find(
    (t) => `${t.matrixId}:${t.documentId}:${t.fieldId}` === target
  );
  const id = claim.claim_id;
  return (
    <div className="mt-1 space-y-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`link-${id}`}>
          Accepted value to link
        </label>
        <select
          id={`link-${id}`}
          value={target}
          onChange={(event) => setTarget(event.target.value)}
          className="min-w-0 rounded-md border border-border bg-background px-2 py-1"
        >
          <option value="">Link an accepted value…</option>
          {targets.map((t) => (
            <option
              key={`${t.matrixId}:${t.documentId}:${t.fieldId}`}
              value={`${t.matrixId}:${t.documentId}:${t.fieldId}`}
            >
              {t.label}
            </option>
          ))}
        </select>
        <Button
          size="sm"
          variant="outline"
          disabled={!picked || link.isPending}
          onClick={() => picked && link.mutate(picked)}
        >
          Link evidence
        </Button>
        {results.length > 0 && (
          <>
            <label className="sr-only" htmlFor={`synthesis-${id}`}>
              Synthesis result to link
            </label>
            <select
              id={`synthesis-${id}`}
              value={resultId}
              onChange={(event) => setResultId(event.target.value)}
              className="min-w-0 rounded-md border border-border bg-background px-2 py-1"
            >
              <option value="">Link a synthesis result…</option>
              {results.map((r) => (
                <option key={r.id} value={r.id}>
                  SMD {r.estimate?.toFixed(2)} ({r.outcome_key}, {r.timepoint})
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="outline"
              disabled={!resultId || linkResult.isPending}
              onClick={() => linkResult.mutate(resultId)}
            >
              Link result
            </Button>
          </>
        )}
        {figures.length > 0 && (
          <>
            <label className="sr-only" htmlFor={`figure-${id}`}>
              Figure to link
            </label>
            <select
              id={`figure-${id}`}
              value={figureId}
              onChange={(event) => setFigureId(event.target.value)}
              className="min-w-0 rounded-md border border-border bg-background px-2 py-1"
            >
              <option value="">Link a figure…</option>
              {figures.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.figure_key} ({f.kind}): {f.caption}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="outline"
              disabled={!figureId || linkFigure.isPending}
              onClick={() => linkFigure.mutate(figureId)}
            >
              Link figure
            </Button>
          </>
        )}
        {live
          .filter((row) => !NO_STANCE.includes(row.kind))
          .map((row) => (
            <Button
              key={row.id}
              size="sm"
              variant="ghost"
              disabled={observe.isPending}
              onClick={() => observe.mutate(row.id)}
            >
              Observe {KIND_LABELS[row.kind].toLowerCase()} link
            </Button>
          ))}
      </div>
      <WriteError
        error={
          link.error ?? linkResult.error ?? linkFigure.error ?? observe.error
        }
      />
      {adjudicator && (
        <fieldset className="space-y-1 rounded-md border border-border p-2">
          <legend className="px-1">Assess</legend>
          <label className="sr-only" htmlFor={`stance-${id}`}>
            Stance
          </label>
          <select
            id={`stance-${id}`}
            value={stance}
            onChange={(event) =>
              setStance(event.target.value as ApiClaimAssessmentStance)
            }
            className="rounded-md border border-border bg-background px-2 py-1"
          >
            {STANCES.map((option) => (
              <option key={option} value={option}>
                {words(option)}
              </option>
            ))}
          </select>
          {live
            .filter((row) => row.kind !== 'legacy_unanchored')
            .map((row) => (
              <label key={row.id} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={chosen.includes(row.id)}
                  onChange={(event) =>
                    setChosen((current) =>
                      event.target.checked
                        ? [...current, row.id]
                        : current.filter((value) => value !== row.id)
                    )
                  }
                />
                {KIND_LABELS[row.kind]} link {row.id.slice(0, 8)}
              </label>
            ))}
          <label className="sr-only" htmlFor={`rationale-${id}`}>
            Rationale
          </label>
          <textarea
            id={`rationale-${id}`}
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            placeholder="Rationale"
            rows={2}
            className="w-full rounded-md border border-border bg-background p-1"
          />
          <Button
            size="sm"
            disabled={!rationale.trim() || assess.isPending}
            onClick={() => assess.mutate(undefined)}
          >
            Record assessment
          </Button>
          <WriteError error={assess.error} />
        </fieldset>
      )}
    </div>
  );
}

async function loadLinkTargets(projectId: string): Promise<LinkTarget[]> {
  const { matrices } = await listMatrices(projectId);
  const grids = await Promise.all(matrices.map((m) => getMatrix(m.id)));
  return grids.flatMap((grid) =>
    grid.cells
      .filter((cell) => cell.source === 'accepted' && cell.field_id)
      .map((cell) => ({
        matrixId: grid.id,
        documentId: cell.document_id,
        fieldId: cell.field_id as string,
        label: `${grid.name} · ${cell.column_name}: ${
          cell.value ?? cell.missingness ?? '—'
        }`,
      }))
  );
}

export function DraftClaimsPanel({
  projectId,
  draftId,
  content,
  roles,
  canEdit = false,
}: {
  projectId: string;
  draftId: string;
  /** The draft version's stored content; required for authoring. */
  content?: string;
  /** The viewer's own workflow roles. */
  roles?: ResearchProjectRole[];
  canEdit?: boolean;
}): ReactElement | null {
  const capabilities = useBackendCapabilities();
  const claims = useDraftClaims(projectId, draftId);
  const authoring = canEdit && content !== undefined;
  const adjudicator = authoring && (roles ?? []).includes('adjudicator');
  const targets = useQuery({
    queryKey: ['project', projectId, 'claims', 'link-targets'],
    queryFn: () => loadLinkTargets(projectId),
    enabled: authoring,
    retry: false,
  });
  const synthesis = useQuery({
    queryKey: ['project', projectId, 'research-engine', 'synthesis'],
    queryFn: () => listSynthesis(projectId),
    enabled: authoring,
    retry: false,
  });
  const results = (synthesis.data?.results ?? []).filter(
    (r) => r.status === 'computed' && !r.stale && !r.superseded
  );
  const figureList = useQuery({
    queryKey: ['project', projectId, 'research-engine', 'figures'],
    queryFn: () => listFigures(projectId),
    enabled: authoring,
    retry: false,
  });
  const figures = (figureList.data?.figures ?? []).filter(
    (f) => !f.stale && !f.superseded
  );
  if (!capabilities.draftClaims || !claims.data) return null;
  const { items, counts } = claims.data;
  return (
    <section
      aria-label="Claims"
      className="space-y-2 rounded-md border border-border p-3 text-sm"
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
      {authoring && (
        <ClaimAuthoring
          projectId={projectId}
          draftId={draftId}
          content={content}
        />
      )}
      {items.length === 0 ? (
        <p className="text-muted-foreground">No claims on this draft yet.</p>
      ) : (
        <ul>
          {items.map((claim) => (
            <ClaimRow key={claim.claim_id} claim={claim} projectId={projectId}>
              {authoring && claim.is_tip && (
                <ClaimControls
                  projectId={projectId}
                  draftId={draftId}
                  claim={claim}
                  targets={targets.data ?? []}
                  results={results}
                  figures={figures}
                  adjudicator={adjudicator}
                />
              )}
            </ClaimRow>
          ))}
        </ul>
      )}
    </section>
  );
}
