'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { History } from 'lucide-react';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useAuth } from '@/hooks/useAuth';
import { listProjectRoles } from '@/services/researchEngineService';
import {
  acceptExtractionValue,
  createObservation,
  getCellObservations,
  listFormVersions,
} from '@/services/scispaceService';
import type {
  ApiExtractionAcceptCreate,
  ApiExtractionAcceptedValue,
  ApiExtractionObservation,
} from '@/types/api/research-extraction-contract';
import { ANCHOR_LABELS, CellCitation } from './CellCitation';

export const MISSINGNESS_LABELS: Record<string, string> = {
  not_reported: 'Not reported',
  not_applicable: 'Not applicable',
  unavailable_text: 'Text unavailable',
  extraction_error: 'Extraction error',
  unresolved_disagreement: 'Unresolved disagreement',
};

const RANK: Record<string, number> = {
  verified: 0,
  ambiguous: 1,
  unverified: 2,
};

function display(value: unknown, missingness?: string | null): string {
  if (missingness) return MISSINGNESS_LABELS[missingness] ?? missingness;
  if (value === null || value === undefined) return '';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function choiceKey(o: ApiExtractionObservation): string {
  return o.missingness ? `m:${o.missingness}` : `v:${JSON.stringify(o.value)}`;
}

function anchorLabel(o: ApiExtractionObservation): string {
  if (!o.anchor) return 'no source location';
  return ANCHOR_LABELS[o.anchor.status] ?? o.anchor.status;
}

function actor(o: ApiExtractionObservation): string {
  return o.kind === 'machine'
    ? `Machine · ${o.extractor_model ?? 'unknown model'}`
    : `Reviewer ${o.actor_user_id.slice(0, 8)}`;
}

function chainTip(chain: ApiExtractionAcceptedValue[]): string | null {
  const superseded = new Set(chain.map((a) => a.supersedes_accepted_value_id));
  const tips = chain.filter((a) => !superseded.has(a.id));
  return tips.length ? tips[tips.length - 1].id : null;
}

function Quoted({
  before,
  quote,
  after,
}: {
  before: string;
  quote: string;
  after: string;
}): React.JSX.Element {
  return (
    <p className="whitespace-pre-wrap break-words text-muted-foreground">
      …{before}
      <mark className="bg-primary/20 text-foreground">{quote}</mark>
      {after}…
    </p>
  );
}

interface CellObservationsProps {
  projectId: string;
  matrixId: string;
  documentId: string;
  fieldId: string;
  column: string;
  citation?: string | null;
  anchorStatus?: string | null;
  /** Called after an accept or a new observation (the grid refetches). */
  onDecided?: () => void;
}

/**
 * GOO-305 evidence drawer for one cell: anchored evidence, coverage, the
 * disagreement view, adjudication (ADJUDICATOR) and new evidence (REVIEWER).
 * Offsets and context are sliced by the server; this never indexes text.
 */
export function CellObservations({
  projectId,
  matrixId,
  documentId,
  fieldId,
  column,
  citation = null,
  anchorStatus = null,
  onDecided,
}: CellObservationsProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [occurrence, setOccurrence] = useState<Record<string, number>>({});
  const [cited, setCited] = useState<string[] | null>(null);
  const [choice, setChoice] = useState('');
  const [rationale, setRationale] = useState('');
  const [confirmUnverified, setConfirmUnverified] = useState(false);
  const [draft, setDraft] = useState({ value: '', citation: '', start: '' });
  const queryClient = useQueryClient();
  const userId = useAuth().user?.id;
  const cellKey = ['extraction-observations', matrixId, documentId, fieldId];

  const { data, isLoading, isError } = useQuery({
    queryKey: cellKey,
    queryFn: () => getCellObservations(matrixId, documentId, fieldId),
    enabled: open,
  });
  const { data: versions } = useQuery({
    queryKey: ['extraction-form-versions', matrixId],
    queryFn: () => listFormVersions(matrixId),
    enabled: open,
  });
  const { data: roles } = useQuery({
    queryKey: ['project', projectId, 'research-engine', 'roles'],
    queryFn: () => listProjectRoles(projectId),
    enabled: open,
    retry: false,
  });
  const mine = new Set(
    (roles ?? []).filter((r) => r.user_id === userId).map((r) => r.role)
  );
  const current = versions?.reduce<(typeof versions)[number] | undefined>(
    (best, v) => (best && best.version_no >= v.version_no ? best : v),
    undefined
  );
  const versionNo = (id: string): string => {
    const n = versions?.find((v) => v.id === id)?.version_no;
    return n === undefined ? '' : `v${n}`;
  };

  const observations = data?.observations ?? [];
  const selected =
    observations.find((o) => o.id === selectedId) ?? observations[0];
  const citedIds = cited ?? (selected ? [selected.id] : []);
  const citedRows = observations.filter((o) => citedIds.includes(o.id));
  const options = new Map<string, string>();
  for (const o of citedRows) {
    if (o.missingness === 'extraction_error') continue;
    if (!o.missingness && o.validation_state !== 'valid') continue;
    options.set(choiceKey(o), display(o.value, o.missingness));
  }
  if (new Set(citedRows.map(choiceKey)).size >= 2) {
    options.set('m:unresolved_disagreement', 'Unresolved disagreement');
  }
  const anchorRow = choice.startsWith('v:')
    ? citedRows
        .filter(
          (o) =>
            !o.missingness &&
            o.validation_state === 'valid' &&
            choiceKey(o) === choice
        )
        .sort(
          (a, b) =>
            (RANK[a.anchor?.status ?? 'unverified'] ?? 3) -
              (RANK[b.anchor?.status ?? 'unverified'] ?? 3) ||
            a.created_at.localeCompare(b.created_at)
        )[0]
    : undefined;
  const anchorState = anchorRow?.anchor?.status ?? 'unverified';
  const needsOccurrence =
    anchorState === 'ambiguous' && anchorRow && !(anchorRow.id in occurrence);
  const needsConfirm =
    anchorRow !== undefined &&
    anchorState !== 'verified' &&
    anchorState !== 'ambiguous';
  const sourceChanged = citedRows.some((o) => o.source_changed);

  const accept = useMutation({
    mutationFn: () =>
      acceptExtractionValue(matrixId, {
        document_id: documentId,
        field_id: fieldId,
        form_version_id: current?.id ?? '',
        observation_ids: citedIds,
        value: choice.startsWith('v:') ? JSON.parse(choice.slice(2)) : null,
        missingness: choice.startsWith('m:')
          ? (choice.slice(2) as ApiExtractionAcceptCreate['missingness'])
          : null,
        rationale: rationale.trim(),
        supersedes_accepted_value_id: chainTip(data?.accepted_chain ?? []),
        anchor_start: anchorRow ? (occurrence[anchorRow.id] ?? null) : null,
        accept_unverified: needsConfirm && confirmUnverified,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      setRationale('');
      setConfirmUnverified(false);
      void queryClient.invalidateQueries({ queryKey: cellKey });
      onDecided?.();
    },
  });
  const observe = useMutation({
    mutationFn: () =>
      createObservation(matrixId, {
        document_id: documentId,
        field_id: fieldId,
        form_version_id: current?.id ?? '',
        value: draft.value,
        citation: draft.citation || null,
        anchor_start: draft.start === '' ? null : Number(draft.start),
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      setDraft({ value: '', citation: '', start: '' });
      void queryClient.invalidateQueries({ queryKey: cellKey });
      onDecided?.();
    },
  });
  const canAccept =
    Boolean(current) &&
    options.has(choice) &&
    rationale.trim().length > 0 &&
    !needsOccurrence &&
    (!needsConfirm || confirmUnverified) &&
    !sourceChanged &&
    !accept.isPending;

  const toggleCite = (id: string, on: boolean): void =>
    setCited(on ? [...citedIds, id] : citedIds.filter((c) => c !== id));

  return (
    <>
      <button
        type="button"
        className="inline-flex items-center justify-center h-5 w-5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
        aria-label={`Observations for ${column}`}
        onClick={() => setOpen(true)}
      >
        <History className="h-3 w-3" />
      </button>
      <CellCitation
        citation_snippet={citation}
        anchorStatus={anchorStatus}
        onOpen={() => setOpen(true)}
      />
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="right"
          className="w-full sm:max-w-2xl overflow-y-auto space-y-5 text-xs"
        >
          <SheetHeader>
            <SheetTitle>{column}</SheetTitle>
            <SheetDescription>
              Evidence, coverage and decisions for this cell.
            </SheetDescription>
          </SheetHeader>
          {isLoading && <p className="text-muted-foreground">Loading…</p>}
          {isError && (
            <p className="text-destructive">Failed to load observations</p>
          )}
          {data && observations.length === 0 && (
            <p className="text-muted-foreground">No observations</p>
          )}

          {selected && (
            <section aria-label="Evidence" className="space-y-2">
              <h3 className="font-medium text-foreground">Evidence</h3>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-foreground">
                  {display(selected.value, selected.missingness)}
                </span>
                <Badge variant="outline">{anchorLabel(selected)}</Badge>
                <span className="text-muted-foreground">{actor(selected)}</span>
              </div>
              {selected.source_changed && (
                <p role="status" className="text-destructive">
                  Source changed since extraction; re-run extraction
                </p>
              )}
              {selected.anchor && selected.citation && (
                <>
                  {selected.context_before != null &&
                  selected.context_after != null ? (
                    <Quoted
                      before={selected.context_before}
                      quote={selected.citation}
                      after={selected.context_after}
                    />
                  ) : (
                    <p className="italic text-muted-foreground">
                      “{selected.citation}”
                    </p>
                  )}
                  <p className="text-muted-foreground">
                    {selected.anchor.page != null
                      ? `p. ${selected.anchor.page}`
                      : 'page unavailable'}
                    {selected.anchor.start_char != null &&
                      ` · chars ${selected.anchor.start_char}–${selected.anchor.end_char} of ${selected.text_length ?? '?'}`}
                  </p>
                </>
              )}
              {selected.anchor?.status === 'ambiguous' && (
                <fieldset className="space-y-2">
                  <legend className="text-muted-foreground">
                    Appears {selected.anchor.occurrences_in_text ?? '?'}× in
                    document; choose the occurrence that supports the value
                  </legend>
                  {(selected.anchor.occurrence_contexts ?? []).map((c) => (
                    <label key={c.start_char} className="flex gap-2">
                      <input
                        type="radio"
                        name={`occurrence-${selected.id}`}
                        checked={occurrence[selected.id] === c.start_char}
                        onChange={() =>
                          setOccurrence({
                            ...occurrence,
                            [selected.id]: c.start_char,
                          })
                        }
                      />
                      <span>
                        {c.page != null ? `p. ${c.page}` : 'page unavailable'}
                        <Quoted
                          before={c.context_before}
                          quote={selected.citation ?? ''}
                          after={c.context_after}
                        />
                      </span>
                    </label>
                  ))}
                </fieldset>
              )}
            </section>
          )}

          {selected?.inspected_coverage && selected.text_length != null && (
            <section aria-label="Coverage" className="space-y-1">
              <h3 className="font-medium text-foreground">Coverage</h3>
              <p className="text-muted-foreground">
                Inspected{' '}
                {selected.text_length
                  ? Math.round(
                      (100 *
                        selected.inspected_coverage.reduce(
                          (sum, [lo, hi]) => sum + (hi - lo),
                          0
                        )) /
                        selected.text_length
                    )
                  : 0}
                % of text (
                {selected.inspected_coverage
                  .map(([lo, hi]) => `${lo}–${hi}`)
                  .join(', ') || 'nothing'}
                )
              </p>
              {selected.coverage_complete === false && (
                <p className="text-destructive">
                  Partial coverage: a missing value may be in unread text
                </p>
              )}
            </section>
          )}

          {observations.length > 0 && (
            <section aria-label="Observations" className="space-y-2">
              <h3 className="font-medium text-foreground">
                {observations.length >= 2 ? 'Disagreement' : 'Observation'}
              </h3>
              <ul
                data-testid="observation-columns"
                className="grid grid-cols-2 gap-3"
              >
                {observations.map((o) => (
                  <li
                    key={o.id}
                    className={`rounded border p-2 space-y-1 ${o.id === selected?.id ? 'border-primary' : 'border-border'}`}
                  >
                    <button
                      type="button"
                      className="text-left w-full"
                      onClick={() => setSelectedId(o.id)}
                    >
                      <p
                        className={
                          o.missingness
                            ? 'italic text-muted-foreground'
                            : 'text-foreground'
                        }
                      >
                        {display(o.value, o.missingness)}
                        {o.validation_state === 'invalid' && ' (unvalidated)'}
                      </p>
                      <p className="text-muted-foreground">
                        {actor(o)} {versionNo(o.form_version_id)}
                      </p>
                      <p className="text-muted-foreground">
                        {anchorLabel(o)} ·{' '}
                        <time dateTime={o.created_at}>
                          {new Date(o.created_at).toLocaleString()}
                        </time>
                      </p>
                    </button>
                    {mine.has('adjudicator') && (
                      <label className="flex items-center gap-1">
                        <Checkbox
                          checked={citedIds.includes(o.id)}
                          onCheckedChange={(on) =>
                            toggleCite(o.id, on === true)
                          }
                        />
                        Cite
                      </label>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {data && data.accepted_chain.length > 0 && (
            <section aria-label="Accepted chain" className="space-y-1">
              <h3 className="font-medium text-foreground">Accepted chain</h3>
              <ol className="space-y-1">
                {data.accepted_chain.map((a) => (
                  <li key={a.id}>
                    <span className="text-foreground">
                      {display(a.value, a.missingness)}
                    </span>{' '}
                    <span className="text-muted-foreground">
                      {versionNo(a.form_version_id)} ·{' '}
                      {ANCHOR_LABELS[a.anchor_resolution ?? 'legacy']} ·{' '}
                      {new Date(a.created_at).toLocaleString()} · {a.rationale}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {data && mine.has('adjudicator') && (
            <section aria-label="Decide" className="space-y-2">
              <h3 className="font-medium text-foreground">Decide</h3>
              {sourceChanged && (
                <p role="status" className="text-destructive">
                  Source changed since extraction; re-run extraction
                </p>
              )}
              <Label htmlFor={`choice-${fieldId}`}>Accepted value</Label>
              <select
                id={`choice-${fieldId}`}
                className="w-full rounded border border-border bg-background p-1"
                value={choice}
                onChange={(e) => {
                  setChoice(e.target.value);
                  const row = citedRows.find(
                    (o) => choiceKey(o) === e.target.value
                  );
                  if (row) setSelectedId(row.id);
                }}
              >
                <option value="">Choose a cited value</option>
                {[...options].map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
              {needsOccurrence && (
                <p className="text-muted-foreground">
                  Choose an occurrence of the citation above to accept it.
                </p>
              )}
              {needsConfirm && (
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={confirmUnverified}
                    onCheckedChange={(on) => setConfirmUnverified(on === true)}
                  />
                  Accept without a verified source location
                </label>
              )}
              <Label htmlFor={`rationale-${fieldId}`}>Rationale</Label>
              <Textarea
                id={`rationale-${fieldId}`}
                required
                value={rationale}
                onChange={(e) => setRationale(e.target.value)}
              />
              <Button
                size="sm"
                disabled={!canAccept}
                onClick={() => accept.mutate()}
              >
                Accept
              </Button>
              {accept.isError && (
                <p role="alert" className="text-destructive">
                  {accept.error instanceof Error
                    ? accept.error.message
                    : 'Accept failed'}
                </p>
              )}
            </section>
          )}

          {data && mine.has('reviewer') && (
            <section aria-label="Add evidence" className="space-y-2">
              <h3 className="font-medium text-foreground">Add evidence</h3>
              <Label htmlFor={`value-${fieldId}`}>Value</Label>
              <Input
                id={`value-${fieldId}`}
                value={draft.value}
                onChange={(e) => setDraft({ ...draft, value: e.target.value })}
              />
              <Label htmlFor={`citation-${fieldId}`}>
                Verbatim citation from the document
              </Label>
              <Input
                id={`citation-${fieldId}`}
                value={draft.citation}
                onChange={(e) =>
                  setDraft({ ...draft, citation: e.target.value })
                }
              />
              <Label htmlFor={`start-${fieldId}`}>
                Occurrence start (optional)
              </Label>
              <Input
                id={`start-${fieldId}`}
                type="number"
                min={0}
                value={draft.start}
                onChange={(e) => setDraft({ ...draft, start: e.target.value })}
              />
              <Button
                size="sm"
                variant="outline"
                disabled={!current || !draft.value.trim() || observe.isPending}
                onClick={() => observe.mutate()}
              >
                Add evidence
              </Button>
              {observe.isError && (
                <p role="alert" className="text-destructive">
                  {observe.error instanceof Error
                    ? observe.error.message
                    : 'Could not add evidence'}
                </p>
              )}
            </section>
          )}
        </SheetContent>
      </Sheet>
    </>
  );
}

export default CellObservations;
