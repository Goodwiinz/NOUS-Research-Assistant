import type { ReactElement } from 'react';
import Papa from 'papaparse';

const MAX_ROWS = 200;
const MAX_COLUMNS = 50;

export function CsvArtifactPreview({ text }: { text: string }): ReactElement {
  const parsed = Papa.parse<string[]>(text, {
    preview: MAX_ROWS + 1,
    skipEmptyLines: 'greedy',
  });
  const rows = parsed.data
    .slice(0, MAX_ROWS)
    .map((row) => row.slice(0, MAX_COLUMNS));
  const truncated =
    parsed.meta.truncated ||
    parsed.data.length > MAX_ROWS ||
    parsed.data.some((row) => row.length > MAX_COLUMNS);
  return (
    <div className="overflow-auto p-4">
      {truncated && (
        <p className="mb-3 text-sm text-muted-foreground">
          Showing the first 200 rows and 50 columns.
        </p>
      )}
      <table aria-label="CSV preview" className="border-collapse text-sm">
        <thead>
          <tr>
            {(rows[0] ?? []).map((cell, index) => (
              <th key={index} scope="col" className="border p-2 text-left">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(1).map((row, index) => (
            <tr key={index}>
              {row.map((cell, column) => (
                <td key={column} className="border p-2">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Bound depth/node count before pretty-printing; raw source is already byte-bounded. */
function boundedJson(
  value: unknown,
  depth: number,
  budget: { remaining: number }
): unknown {
  if (budget.remaining-- <= 0) return '[node limit]';
  if (value === null || typeof value !== 'object')
    return typeof value === 'string' && value.length > 2000
      ? `${value.slice(0, 2000)}…`
      : value;
  if (depth >= 20) return '[depth limit]';
  if (Array.isArray(value))
    return value
      .slice(0, 200)
      .map((item) => boundedJson(item, depth + 1, budget));
  return Object.fromEntries(
    Object.entries(value)
      .slice(0, 200)
      .map(([key, item]) => [key, boundedJson(item, depth + 1, budget)])
  );
}

export function JsonArtifactPreview({ text }: { text: string }): ReactElement {
  let pretty: string;
  try {
    pretty = JSON.stringify(
      boundedJson(JSON.parse(text), 0, { remaining: 2000 }),
      null,
      2
    );
  } catch {
    return (
      <div className="p-4">
        <p className="mb-3 text-sm">
          This file is not valid JSON. Showing source.
        </p>
        <pre className="whitespace-pre-wrap break-words text-sm">{text}</pre>
      </div>
    );
  }
  return (
    <div className="p-4">
      <pre
        aria-label="JSON preview"
        className="overflow-auto whitespace-pre-wrap break-words text-sm"
      >
        {pretty}
      </pre>
      <details className="mt-3">
        <summary>Source</summary>
        <pre className="whitespace-pre-wrap break-words text-sm">{text}</pre>
      </details>
    </div>
  );
}
