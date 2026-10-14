import type { PageFilter } from '@/types/pdf-chat';

/** Only server-authored diagnostics. Absence is not a claim about filtering. */
export function PageFilterDetails({
  filter,
}: {
  filter: PageFilter;
}): React.ReactElement {
  const selected = filter.selection_page_range;
  return (
    <details className="mb-3 rounded-md border border-(--nous-border-1) px-3 py-2 text-xs text-(--nous-fg-2)">
      <summary className="cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)">
        {selected ? 'Selected PDF pages' : 'PDF pages used'}:{' '}
        {filter.accepted_pages.join(', ') || 'none'} of {filter.total_pages}
      </summary>
      <div className="mt-2 space-y-1">
        {selected && (
          <p>
            Requested pages {selected[0] + 1}–{selected[1] + 1} (inclusive).
          </p>
        )}
        <p>
          {filter.short_circuited
            ? 'Page ranking was not needed.'
            : 'Pages were ranked for this question.'}
        </p>
        {filter.model && <p>Page ranking model: {filter.model}</p>}
        {filter.timing_ms && (
          <p>
            Average ranking time: {Math.round(filter.timing_ms.average)} ms.
          </p>
        )}
      </div>
    </details>
  );
}
