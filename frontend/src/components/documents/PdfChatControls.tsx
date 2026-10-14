'use client';

import { useId, useState } from 'react';
import {
  parsePdfPageRange,
  type PdfAskHandler,
  type PdfTextSelection,
} from '@/types/pdf-chat';

export function PdfChatControls({
  onAsk,
  textSelection,
  pageCount,
  pending = false,
}: {
  onAsk: PdfAskHandler;
  textSelection?: PdfTextSelection;
  pageCount?: number;
  pending?: boolean;
}): React.ReactElement {
  const id = useId();
  const [first, setFirst] = useState('');
  const [last, setLast] = useState('');
  const [quote, setQuote] = useState(textSelection?.quote);
  const [appliedSelection, setAppliedSelection] = useState(textSelection);
  if (appliedSelection !== textSelection) {
    setAppliedSelection(textSelection);
    setQuote(textSelection?.quote);
  }
  const selection = parsePdfPageRange(first, last);
  const error =
    selection.error ||
    (selection.range && pageCount && selection.range[1] >= pageCount
      ? `This PDF has ${pageCount} pages.`
      : undefined);

  return (
    <div className="space-y-2 border-b border-border px-4 py-3">
      <p id={`${id}-help`} className="text-xs text-muted-foreground">
        Ask about this PDF only. Leave both fields blank to use the PDF, or
        enter an inclusive page range to use only those pages. Selecting PDF
        text includes the passage as a quote without restricting pages. You can
        clear the quote and page range separately.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs font-medium" htmlFor={`${id}-first`}>
          First page
          <input
            id={`${id}-first`}
            type="number"
            min={1}
            step={1}
            value={first}
            disabled={pending}
            onChange={(event) => {
              setFirst(event.target.value);
            }}
            aria-describedby={`${id}-help${error ? ` ${id}-error` : ''}`}
            aria-invalid={Boolean(error)}
            className="mt-1 block min-h-11 w-24 rounded-md border border-border bg-background px-2 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <label className="text-xs font-medium" htmlFor={`${id}-last`}>
          Last page
          <input
            id={`${id}-last`}
            type="number"
            min={1}
            step={1}
            value={last}
            disabled={pending}
            onChange={(event) => {
              setLast(event.target.value);
            }}
            aria-describedby={`${id}-help${error ? ` ${id}-error` : ''}`}
            aria-invalid={Boolean(error)}
            className="mt-1 block min-h-11 w-24 rounded-md border border-border bg-background px-2 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <button
          type="button"
          disabled={Boolean(error) || pending}
          aria-busy={pending}
          onClick={() =>
            quote
              ? onAsk(selection.range, quote, textSelection?.range)
              : onAsk(selection.range)
          }
          className="min-h-11 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        >
          {pending ? 'Opening PDF chat…' : 'Ask about PDF'}
        </button>
        {(first || last) && (
          <button
            type="button"
            disabled={pending}
            onClick={() => {
              setFirst('');
              setLast('');
            }}
            className="min-h-11 rounded-md border border-border px-3 text-xs focus-visible:ring-2 focus-visible:ring-ring"
          >
            Clear page range
          </button>
        )}
      </div>
      {quote && (
        <div className="space-y-2">
          {textSelection && (
            <p
              aria-label="Selected PDF pages"
              className="text-xs text-muted-foreground"
            >
              Quoted from pages {textSelection.range[0] + 1}–
              {textSelection.range[1] + 1}
            </p>
          )}
          <blockquote
            aria-label="Selected PDF passage"
            className="max-h-32 overflow-auto whitespace-pre-wrap border-l-2 border-border pl-3 text-xs"
          >
            {quote}
          </blockquote>
          <button
            type="button"
            disabled={pending}
            onClick={() => setQuote(undefined)}
            className="min-h-11 rounded-md border border-border px-3 text-xs focus-visible:ring-2 focus-visible:ring-ring"
          >
            Clear quote
          </button>
        </div>
      )}
      {error && (
        <p
          id={`${id}-error`}
          role="status"
          className="text-xs text-destructive"
        >
          {error}
        </p>
      )}
    </div>
  );
}
