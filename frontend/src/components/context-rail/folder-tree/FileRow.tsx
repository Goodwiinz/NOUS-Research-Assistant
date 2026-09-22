'use client';

import { BookOpen, FileText, NotebookPen, PenSquare } from 'lucide-react';
import type { FileNode } from './types';

const ICON_MAP = {
  doc: FileText,
  pdf: FileText,
  book: BookOpen,
  note: NotebookPen,
  draft: PenSquare,
} as const;

export function FileRow({ node }: { node: FileNode }) {
  const Icon = ICON_MAP[node.icon] ?? FileText;
  return (
    <li>
      <button
        type="button"
        onClick={node.onSelect}
        className="flex w-full items-center gap-3 py-1 text-left transition-colors hover:opacity-90"
        style={{ fontFamily: 'var(--nous-font-body)' }}
      >
        <Icon
          aria-hidden="true"
          className="h-4 w-4 shrink-0"
          style={{ color: 'var(--nous-fg-3)' }}
        />
        <span
          className="flex-1 truncate text-[14px]"
          style={{ color: 'var(--nous-fg-1)' }}
          title={node.label}
        >
          {node.label}
        </span>
        {node.meta && (
          <span className="shrink-0 text-[11px] text-(--nous-fg-3)">
            {node.meta}
          </span>
        )}
      </button>
    </li>
  );
}
