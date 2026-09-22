'use client';

import type { CitationItem } from '@/hooks';
import { Folder } from 'lucide-react';
import { CollapsibleCard } from './CollapsibleCard';
import { FolderTree, type Node } from './folder-tree';
import { useProjectWorkingFolders } from './hooks/useProjectWorkingFolders';

export type WorkingFoldersSelection =
  | { kind: 'document'; id: string; title: string }
  // `source` carries the citation's URL for non-arXiv web sources — the
  // artifact panel's outbound link needs it (the id alone isn't a URL).
  | { kind: 'external'; id: string; title: string; source?: string }
  | { kind: 'note'; id: string; title: string }
  | { kind: 'draft'; id: string; title: string };

interface WorkingFoldersPanelProps {
  allCitations: CitationItem[];
  projectId?: string;
  workspaceName?: string | null;
  onSelect?: (node: WorkingFoldersSelection) => void;
}

export function WorkingFoldersPanel({
  allCitations,
  projectId,
  workspaceName: _workspaceName,
  onSelect,
}: WorkingFoldersPanelProps) {
  const { documents, notes, drafts, isLoading, errors, refetch } =
    useProjectWorkingFolders(projectId);

  const hasError = Object.values(errors).some(Boolean);

  const threadInternal = allCitations.filter((c) => c.documentId);
  const threadExternal = allCitations.filter((c) => !c.documentId);

  const thisThread: Node = {
    kind: 'folder',
    id: 'this-thread',
    label: 'This thread',
    defaultOpen: true,
    badge:
      threadInternal.length + threadExternal.length > 0
        ? String(threadInternal.length + threadExternal.length)
        : undefined,
    children: [
      ...threadInternal.map((c, i) => ({
        kind: 'file' as const,
        id: `ti-${c.id || c.documentId || i}`,
        label: c.title || 'Untitled document',
        icon: 'pdf' as const,
        onSelect: () =>
          onSelect?.({
            kind: 'document',
            id: (c.documentId ?? '') as string,
            title: c.title || 'Untitled document',
          }),
      })),
      ...threadExternal.map((c, i) => ({
        kind: 'file' as const,
        id: `te-${c.id || c.externalReferenceId || i}`,
        label: c.title || 'Untitled source',
        icon: 'book' as const,
        onSelect: () =>
          onSelect?.({
            kind: 'external',
            id: (c.externalReferenceId ?? c.id ?? '') as string,
            title: c.title || 'Untitled source',
            ...(c.source ? { source: c.source } : {}),
          }),
      })),
    ],
  };

  const tree: Node[] = [thisThread];

  if (projectId && documents && documents.length > 0) {
    tree.push({
      kind: 'folder',
      id: 'sources',
      label: 'Sources',
      defaultOpen: true,
      badge: String(documents.length),
      children: documents.map((d) => ({
        kind: 'file',
        id: `src-${d.id}`,
        label: d.title ?? 'Untitled document',
        icon: 'pdf',
        onSelect: () =>
          onSelect?.({
            kind: 'document',
            id: d.id,
            title: d.title ?? 'Untitled document',
          }),
      })),
    });
  }

  if (projectId && notes && notes.length > 0) {
    const sorted = [...notes].sort(
      (a, b) => Number(b.isPinned ?? 0) - Number(a.isPinned ?? 0)
    );
    tree.push({
      kind: 'folder',
      id: 'notes',
      label: 'Notes',
      badge: String(notes.length),
      children: sorted.map((n) => ({
        kind: 'file',
        id: `note-${n.id}`,
        label: n.title,
        icon: 'note',
        meta: n.isPinned ? 'Pinned' : undefined,
        onSelect: () => onSelect?.({ kind: 'note', id: n.id, title: n.title }),
      })),
    });
  }

  if (projectId && drafts && drafts.length > 0) {
    tree.push({
      kind: 'folder',
      id: 'drafts',
      label: 'Drafts',
      badge: String(drafts.length),
      children: drafts.map((d) => ({
        kind: 'file',
        id: `draft-${d.id}`,
        label: d.title,
        icon: 'draft',
        meta: d.version ? `v${d.version}` : undefined,
        onSelect: () => onSelect?.({ kind: 'draft', id: d.id, title: d.title }),
      })),
    });
  }

  const totalFiles =
    threadInternal.length +
    threadExternal.length +
    (documents?.length ?? 0) +
    (notes?.length ?? 0) +
    (drafts?.length ?? 0);

  return (
    <CollapsibleCard
      title="Working folders"
      icon={<Folder className="h-3 w-3" strokeWidth={1.7} />}
      badge={totalFiles > 0 ? String(totalFiles) : undefined}
    >
      {isLoading ? (
        <div role="status" aria-live="polite" className="py-2">
          <span className="sr-only">Loading project files</span>
          <div
            aria-hidden
            className="h-3 w-2/3 animate-pulse rounded bg-(--nous-bg-2)"
          />
        </div>
      ) : hasError ? (
        <div role="alert" className="py-2">
          <p className="text-[11px] text-(--nous-fg-2)">
            Couldn&apos;t load project files.
          </p>
          <button
            type="button"
            onClick={refetch}
            className="mt-1.5 rounded border border-(--nous-border-1) px-2 py-1 text-[11px] text-(--nous-fg-2) transition-colors hover:bg-(--nous-bg-2) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
          >
            Retry
          </button>
        </div>
      ) : totalFiles === 0 ? (
        <p className="py-2 text-[11px] text-(--nous-fg-3)">
          No files yet. Cited sources will appear here.
        </p>
      ) : (
        <FolderTree nodes={tree} />
      )}
    </CollapsibleCard>
  );
}
