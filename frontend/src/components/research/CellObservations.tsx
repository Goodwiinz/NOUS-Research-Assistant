'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { History } from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  getCellObservations,
  listFormVersions,
} from '@/services/scispaceService';

export const MISSINGNESS_LABELS: Record<string, string> = {
  not_reported: 'Not reported',
  not_applicable: 'Not applicable',
  unavailable_text: 'Text unavailable',
  extraction_error: 'Extraction error',
  unresolved_disagreement: 'Unresolved disagreement',
};

function display(value: unknown, missingness?: string | null): string {
  if (missingness) return MISSINGNESS_LABELS[missingness] ?? missingness;
  if (value === null || value === undefined) return '';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

interface CellObservationsProps {
  matrixId: string;
  documentId: string;
  fieldId: string;
  column: string;
}

/** Read-only history of one cell; reconciliation UI belongs to GOO-305. */
export function CellObservations({
  matrixId,
  documentId,
  fieldId,
  column,
}: CellObservationsProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ['extraction-observations', matrixId, documentId, fieldId],
    queryFn: () => getCellObservations(matrixId, documentId, fieldId),
    enabled: open,
  });
  const { data: versions } = useQuery({
    queryKey: ['extraction-form-versions', matrixId],
    queryFn: () => listFormVersions(matrixId),
    enabled: open,
  });
  const versionNo = (id: string): string => {
    const n = versions?.find((v) => v.id === id)?.version_no;
    return n === undefined ? '' : `v${n}`;
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          className="inline-flex items-center justify-center h-5 w-5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
          aria-label={`Observations for ${column}`}
        >
          <History className="h-3 w-3" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-96 bg-card border-border p-4 space-y-3 text-xs"
        side="top"
        align="start"
      >
        {isLoading && <p className="text-muted-foreground">Loading…</p>}
        {isError && (
          <p className="text-destructive">Failed to load observations</p>
        )}
        {data && (
          <>
            <ul className="space-y-2">
              {data.observations.length === 0 && (
                <li className="text-muted-foreground">No observations</li>
              )}
              {data.observations.map((o) => (
                <li key={o.id} className="border-b border-border pb-1">
                  <div className="flex justify-between gap-2 text-muted-foreground">
                    <span>
                      {o.kind === 'machine'
                        ? `Machine · ${o.extractor_model ?? 'unknown model'}`
                        : `Reviewer ${o.actor_user_id.slice(0, 8)}`}{' '}
                      {versionNo(o.form_version_id)}
                    </span>
                    <time dateTime={o.created_at}>
                      {new Date(o.created_at).toLocaleString()}
                    </time>
                  </div>
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
                </li>
              ))}
            </ul>
            {data.accepted_chain.length > 0 && (
              <div>
                <p className="font-medium text-foreground mb-1">
                  Accepted chain
                </p>
                <ol className="space-y-1">
                  {data.accepted_chain.map((a) => (
                    <li key={a.id}>
                      <span className="text-foreground">
                        {display(a.value, a.missingness)}
                      </span>{' '}
                      <span className="text-muted-foreground">
                        {versionNo(a.form_version_id)} ·{' '}
                        {new Date(a.created_at).toLocaleString()} ·{' '}
                        {a.rationale}
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}

export default CellObservations;
