import type { components } from '@/types/generated/api';

export type PdfPageRange = NonNullable<
  components['schemas']['AgentExecuteRequest']['selection_page_range']
>;
export type PageFilter = components['schemas']['PageFilterResponse'];

/** Canonical branch edit/retry intent, never a runtime or idempotency ID. */
export interface PdfBranchIntent {
  parentMessageId: string | null;
  replacedMessageId: string;
  selectedHeadMessageId: string | null;
}

/** Composer intent only. The backend remains the owner of document access and
 * persisted turns; this value is discarded when the chat scope changes. */
export interface PdfChatContext {
  documentId: string;
  title: string;
  /** Only a range explicitly entered by the user restricts retrieval. */
  selectionPageRange?: PdfPageRange;
  /** Bounded Markdown quote from this PDF's text layer; never persisted as intent. */
  selectionQuote?: string;
  /** Quote provenance only; never an implicit retrieval restriction. */
  selectionQuoteRange?: PdfPageRange;
}

export interface PdfTextSelection {
  range: PdfPageRange;
  quote: string;
}

/** False leaves the selection in its viewer for retry; void accepts a sync handoff. */
export type PdfAskHandler = (
  range?: PdfPageRange,
  quote?: string,
  quoteRange?: PdfPageRange
) => boolean | void | Promise<boolean | void>;

/** alphaXiv's shipped client bounds the expanded quote, before its prefix. */
export function quotePdfSelection(text: string): string {
  const expanded = text.replace(/\n/g, '\n> ');
  const bounded =
    expanded.length > 2000
      ? `${expanded.slice(0, 997)}...${expanded.slice(-997)}`
      : expanded;
  return `> ${bounded}`;
}

/** Matches client normalization, including surrogate halves cut by truncation. */
export function composePdfQuestion(question: string, quote: string): string {
  return Array.from(`${quote}\n\n${question}`)
    .filter((character) => {
      const unit = character.charCodeAt(0);
      return (
        character !== '\0' &&
        character !== '\uFFFD' &&
        !(character.length === 1 && unit >= 0xd800 && unit <= 0xdfff)
      );
    })
    .join('');
}

/** Human page numbers are converted at this input boundary, exactly once. */
export function parsePdfPageRange(
  first: string,
  last: string
): { range?: PdfPageRange; error?: string } {
  if (!first.trim() && !last.trim()) return {};
  if (!/^[1-9]\d*$/.test(first) || !/^[1-9]\d*$/.test(last)) {
    return {
      error: 'Enter a whole page number of 1 or greater in both fields.',
    };
  }
  const start = Number(first);
  const end = Number(last);
  if (
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(end) ||
    end < start
  ) {
    return {
      error: 'The last page must be the same as or after the first page.',
    };
  }
  if (end - start >= 100) {
    return { error: 'Choose no more than 100 pages at a time.' };
  }
  return { range: [start - 1, end - 1] };
}

const isPage = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 1;
const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

type PageFilterFallback = NonNullable<PageFilter['fallback']>;
const PAGE_FILTER_FALLBACKS: readonly PageFilterFallback[] = [
  'disabled',
  'provider_error',
  'timeout',
  'no_results',
  'no_text',
  'source_too_large',
];

/** SSE bodies are untyped JSON. Project only the bounded public contract;
 * malformed or older-server metadata never becomes a claimed page selection. */
export function parsePageFilter(value: unknown): PageFilter | undefined {
  if (!value || typeof value !== 'object') return;
  const data = value as Record<string, unknown>;
  if (
    typeof data.document_id !== 'string' ||
    !data.document_id ||
    !Array.isArray(data.accepted_pages) ||
    data.accepted_pages.length > 100 ||
    !data.accepted_pages.every(isPage) ||
    typeof data.total_pages !== 'number' ||
    !Number.isSafeInteger(data.total_pages) ||
    data.total_pages < 0 ||
    typeof data.short_circuited !== 'boolean'
  )
    return;

  const selection = data.selection_page_range;
  const range =
    Array.isArray(selection) &&
    selection.length === 2 &&
    selection.every((page) => Number.isSafeInteger(page) && page >= 0) &&
    selection[1] >= selection[0]
      ? ([selection[0], selection[1]] as PdfPageRange)
      : undefined;
  const timing = data.timing_ms as Record<string, unknown> | null | undefined;
  const validTiming =
    timing &&
    [timing.average, timing.min, timing.max, timing.count].every(
      (number) => isFiniteNumber(number) && number >= 0
    ) &&
    Number.isSafeInteger(timing.count);
  return {
    document_id: data.document_id,
    accepted_pages: data.accepted_pages,
    total_pages: data.total_pages,
    short_circuited: data.short_circuited,
    priorities: Array.isArray(data.priorities)
      ? data.priorities.slice(0, 100).flatMap((item: unknown) => {
          if (!item || typeof item !== 'object') return [];
          const entry = item as Record<string, unknown>;
          return isPage(entry.page) && isFiniteNumber(entry.score)
            ? [{ page: entry.page, score: entry.score }]
            : [];
        })
      : [],
    model:
      typeof data.model === 'string' && data.model.length <= 128
        ? data.model
        : null,
    selection_page_range: range ?? null,
    fallback: PAGE_FILTER_FALLBACKS.includes(data.fallback as PageFilterFallback)
      ? (data.fallback as PageFilterFallback)
      : null,
    timing_ms: validTiming
      ? {
          average: timing.average as number,
          min: timing.min as number,
          max: timing.max as number,
          count: timing.count as number,
        }
      : null,
  };
}
