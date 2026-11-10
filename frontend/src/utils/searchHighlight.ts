/**
 * Search-result snippets arrive from the backend with `<mark>…</mark>` around
 * the matched terms (full-text search highlighting). These helpers turn that
 * string into plain segments so callers can render real `<mark>` elements, or
 * strip the tags where the snippet is used as plain text (labels, previews,
 * filters). Only `<mark>` and `</mark>` (any letter case) are recognised; every
 * other tag and every HTML entity stays literal text. Nothing here produces
 * HTML, so the output is safe to render as React text children.
 */

export interface HighlightSegment {
  text: string;
  highlighted: boolean;
}

const MARK_TAG = /<(\/?)mark\s*>/gi;

export function parseHighlightedSnippet(
  snippet: string | null | undefined
): HighlightSegment[] {
  if (!snippet) return [];

  const segments: HighlightSegment[] = [];
  let depth = 0;
  let cursor = 0;

  const push = (text: string) => {
    if (!text) return;
    const highlighted = depth > 0;
    const last = segments[segments.length - 1];
    if (last && last.highlighted === highlighted) {
      last.text += text;
    } else {
      segments.push({ text, highlighted });
    }
  };

  for (const match of snippet.matchAll(MARK_TAG)) {
    push(snippet.slice(cursor, match.index));
    cursor = (match.index ?? 0) + match[0].length;
    if (match[1] === '/') {
      // A stray closing tag with nothing open is dropped, not shown.
      if (depth > 0) depth -= 1;
    } else {
      depth += 1;
    }
  }
  // An unclosed <mark> highlights to the end of the snippet.
  push(snippet.slice(cursor));

  return segments;
}

export function stripHighlightTags(snippet: string): string;
export function stripHighlightTags(
  snippet: string | null | undefined
): string | undefined;
export function stripHighlightTags(
  snippet: string | null | undefined
): string | undefined {
  if (snippet == null) return undefined;
  return snippet.replace(MARK_TAG, '');
}
