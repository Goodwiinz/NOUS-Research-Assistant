'use client';

import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useSyncExternalStore } from 'react';
import {
  AlertTriangle,
  ExternalLink,
  ListTree,
  Pin,
  RefreshCw,
  PinOff,
  X,
} from 'lucide-react';
import Link from 'next/link';

import { IconButton } from '@/components/ui/icon-button';
import { ChatMarkdown } from '@/components/chat/ChatMarkdown';
import { CitationRenderer } from '@/components/chat/CitationRenderer';
import { CitationPanelBody } from '@/components/chat/CitationPanelBody';
import {
  useDraftArtifact,
  useDraftCitations,
  useNoteArtifact,
} from '@/components/chat/artifact-panel/useArtifactContent';
import { DocumentInlineViewer } from '@/components/documents/DocumentInlineViewer';
import { cn } from '@/lib/utils';
import { documentService } from '@/services/documentService';
import type { DraftCitation } from '@/services/projectService';
import {
  useArtifactPanelStore,
  type Artifact,
} from '@/store/artifactPanelStore';
import type { Citation } from '@/utils/citationParser';

// Version suffix included: the backend stores versioned external references
// (e.g. 2512.14313v1) and the cited revision can differ materially from the
// latest one, so the link must open exactly what was cited.
const ARXIV_RE = /\d{4}\.\d{4,5}(v\d+)?/;

function externalHref(
  artifact: Extract<Artifact, { kind: 'external' }>
): string | undefined {
  const arxivId = artifact.id.match(ARXIV_RE)?.[0];
  if (arxivId) return `https://arxiv.org/abs/${arxivId}`;
  if (artifact.source && /^https?:\/\//.test(artifact.source))
    return artifact.source;
  return undefined;
}

const KIND_LABEL: Record<Artifact['kind'], string> = {
  document: 'Document',
  external: 'External',
  note: 'Note',
  draft: 'Draft',
  citations: 'Sources',
};

function artifactTitle(artifact: Artifact): string {
  return artifact.kind === 'citations' ? 'Sources' : artifact.title;
}

function DocumentArtifactBody({
  documentId,
}: {
  documentId: string;
}): React.ReactElement {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['document', documentId],
    queryFn: () => documentService.getDocument(documentId),
  });

  if (isLoading) {
    return (
      <div className="p-3">
        <div className="h-[60vh] min-h-[320px] animate-pulse rounded-xl border border-(--nous-border-1) bg-(--nous-bg-2)" />
        <span className="sr-only" role="status">
          Loading document…
        </span>
      </div>
    );
  }

  if (isError || !data) {
    return (
      <ArtifactContentError
        what="document"
        onRetry={() => {
          void refetch();
        }}
      />
    );
  }

  return (
    <div className="p-3">
      <DocumentInlineViewer
        documentId={data.id}
        filename={data.filename || data.title}
        kind={data.file_type || data.document_type}
        mimeType={data.mime_type}
      />
    </div>
  );
}

function ExternalArtifactBody({
  artifact,
}: {
  artifact: Extract<Artifact, { kind: 'external' }>;
}): React.ReactElement {
  const href = externalHref(artifact);
  return (
    <div className="m-3 flex min-h-[200px] flex-col items-center justify-center rounded-xl border border-dashed border-(--nous-border-1) bg-(--nous-bg-2) px-6 py-10 text-center">
      <p className="font-nous-ui text-sm font-medium text-(--nous-fg-1)">
        {artifact.title}
      </p>
      <p className="mt-1 max-w-sm text-xs text-(--nous-fg-3)">
        This source lives outside your corpus, so it can&apos;t be previewed
        inline.
      </p>
      {href && (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-5 inline-flex items-center gap-2 rounded-lg border border-(--nous-border-1) bg-(--nous-bg-1) px-4 py-2 text-sm font-medium text-(--nous-fg-2) transition-colors hover:bg-(--nous-aurum) hover:text-(--nous-fg-1) dark:hover:bg-(--nous-ember) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        >
          Open source
          <ExternalLink aria-hidden="true" className="h-4 w-4" />
        </a>
      )}
    </div>
  );
}

function ArtifactContentSkeleton(): React.ReactElement {
  return (
    <div className="p-3">
      <div className="h-[50vh] min-h-[280px] animate-pulse rounded-xl border border-(--nous-border-1) bg-(--nous-bg-2)" />
      <span className="sr-only" role="status">
        Loading…
      </span>
    </div>
  );
}

function ArtifactContentError({
  what,
  onRetry,
}: {
  what: string;
  onRetry?: () => void;
}): React.ReactElement {
  return (
    <div
      role="alert"
      className="m-3 flex min-h-[200px] flex-col items-center justify-center rounded-xl border border-destructive/30 bg-destructive/5 px-6 text-center"
    >
      <AlertTriangle
        aria-hidden="true"
        className="mb-3 h-8 w-8 text-destructive"
      />
      <p className="text-sm font-medium text-(--nous-fg-1)">
        Couldn&apos;t load this {what}
      </p>
      <p className="mt-1 max-w-sm text-xs text-(--nous-fg-3)">
        It may have been deleted, or you may not have access to it.
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 inline-flex items-center gap-2 rounded-lg border border-(--nous-border-1) bg-(--nous-bg-1) px-4 py-2 text-sm font-medium text-(--nous-fg-2) transition-colors hover:bg-(--nous-bg-2) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        >
          <RefreshCw aria-hidden="true" className="h-4 w-4" />
          Retry
        </button>
      )}
    </div>
  );
}

function NoteArtifactBody({
  artifact,
}: {
  artifact: Extract<Artifact, { kind: 'note' }>;
}): React.ReactElement {
  const { data, isLoading, isError, refetch } = useNoteArtifact(
    artifact.projectId,
    artifact.id
  );
  if (isLoading) return <ArtifactContentSkeleton />;
  if (isError || !data)
    return (
      <ArtifactContentError
        what="note"
        onRetry={() => {
          void refetch();
        }}
      />
    );
  return (
    <div className="nous-prose p-4 font-nous-body text-sm leading-relaxed text-(--nous-fg-1)">
      <ChatMarkdown content={data.content} />
    </div>
  );
}

function DraftArtifactBody({
  artifact,
  onCitationClick,
}: {
  artifact: Extract<Artifact, { kind: 'draft' }>;
  onCitationClick: (citation: Citation) => void;
}): React.ReactElement {
  const { data, isLoading, isError, refetch } = useDraftArtifact(
    artifact.projectId,
    artifact.id
  );
  const { data: citationData } = useDraftCitations(
    artifact.projectId,
    artifact.id
  );
  if (isLoading) return <ArtifactContentSkeleton />;
  if (isError || !data)
    return (
      <ArtifactContentError
        what="draft"
        onRetry={() => {
          void refetch();
        }}
      />
    );
  return (
    <div className="p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2 font-nous-mono text-[10px] text-(--nous-fg-3)">
        <span
          className="rounded-sm border border-(--nous-border-1) bg-(--nous-bg-2) px-1.5 py-0.5 uppercase"
          style={{ letterSpacing: '0.08em' }}
        >
          v{data.version}
          {data.is_current ? ' · current' : ''}
        </span>
        {typeof data.word_count === 'number' && (
          <span className="tabular-nums">{data.word_count} words</span>
        )}
        {typeof data.citation_count === 'number' && (
          <span className="tabular-nums">{data.citation_count} citations</span>
        )}
      </div>
      <CitationRenderer
        content={data.content}
        citations={draftCitationsByIndex(citationData?.citations ?? [])}
        onCitationClick={onCitationClick}
        className="font-nous-body text-sm leading-relaxed text-(--nous-fg-1)"
      />
    </div>
  );
}

function draftCitationsByIndex(citations: DraftCitation[]): Citation[] {
  const indexed: Citation[] = [];
  for (const citation of citations) {
    indexed[citation.citation_index - 1] = {
      title: `Document ${citation.citation_index}`,
      documentId: citation.document_id,
      externalReferenceId: citation.citation_id,
      content: citation.context || citation.snippet || '',
      score: 0,
    };
  }
  return indexed;
}

interface ArtifactPanelProps {
  artifact: Artifact;
  /**
   * Codex-style context toggle: the docked panel occupies the rail's slot,
   * so the rail becomes an overlay behind this button. Omitted on viewports
   * where the rail never rendered.
   */
  onToggleRail?: () => void;
  railOpen?: boolean;
  className?: string;
}

/**
 * The chat split-view's right pane: shows the artifact in focus (document,
 * external source — notes/drafts/citations arrive in follow-up PRs) while the
 * chat column stays mounted beside it. Docked at lg+; a bottom sheet below.
 */
// Below `md` the panel is a bottom sheet over the transcript, not a docked
// column, so it needs modal semantics, a backdrop and focus management. The
// breakpoint has to be known in JS (not just CSS) to decide that, and
// useSyncExternalStore keeps it out of an effect.
const SHEET_MEDIA_QUERY = '(max-width: 767.98px)';
const FOCUSABLE_SELECTOR =
  'a[href],button:not([disabled]),input,textarea,select,[tabindex]:not([tabindex="-1"])';

function subscribeSheet(onChange: () => void): () => void {
  const mq = window.matchMedia(SHEET_MEDIA_QUERY);
  mq.addEventListener('change', onChange);
  return () => mq.removeEventListener('change', onChange);
}

function useIsArtifactSheet(): boolean {
  return useSyncExternalStore(
    subscribeSheet,
    () => window.matchMedia(SHEET_MEDIA_QUERY).matches,
    () => false
  );
}

export function ArtifactPanel({
  artifact,
  onToggleRail,
  railOpen,
  className,
}: ArtifactPanelProps): React.ReactElement {
  const closePanel = useArtifactPanelStore((s) => s.closePanel);
  const isSheet = useIsArtifactSheet();
  const sheetRef = useRef<HTMLElement>(null);
  const pinned = useArtifactPanelStore((s) => s.pinned);
  const togglePin = useArtifactPanelStore((s) => s.togglePin);
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);

  // Escape closes the panel. Handling it on the <aside> only worked while
  // focus was already inside the panel — opening it never moves focus (it
  // stays in the composer), so Escape did nothing. The panel is mounted only
  // while open, so the listener's lifetime is the open state. Keep this on
  // the document because focus can remain outside the panel, and let an
  // overlay consume Escape when the event or its live DOM state identifies it.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return;
      // The panel is the bottom layer: anything stacked above it (command
      // palette, a dialog) owns Escape first. Radix can remove its portal
      // during dispatch, so check defaultPrevented and the original event
      // path before consulting the live DOM.
      const eventOverlaySelector =
        '[data-dismissable-overlay], [role="dialog"]';
      const liveOverlaySelector =
        '[data-dismissable-overlay], [role="dialog"][data-state="open"]';
      const eventPathHasOverlay = event
        .composedPath()
        .some(
          (target) =>
            target instanceof Element && target.matches(eventOverlaySelector)
        );
      if (
        event.defaultPrevented ||
        eventPathHasOverlay ||
        document.querySelector(liveOverlaySelector)
      ) {
        return;
      }
      closePanel();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [closePanel]);

  // In sheet mode the panel covers 85dvh but focus stayed in the composer
  // behind it, so tabbing walked obscured controls. Move focus in on open and
  // hand it back on close.
  useEffect(() => {
    if (!isSheet) return;
    const previous = document.activeElement as HTMLElement | null;
    sheetRef.current?.focus();
    return () => previous?.focus?.();
  }, [isSheet]);

  // Focus moved in on open, but Tab still walked out of the sheet into the
  // obscured composer. Wrap at both ends so the modal sheet keeps focus.
  const handleSheetKeyDown = (
    event: React.KeyboardEvent<HTMLElement>
  ): void => {
    if (!isSheet || event.key !== 'Tab' || !sheetRef.current) return;
    const focusables = Array.from(
      sheetRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
    );
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (event.shiftKey ? active === first : active === last) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  };

  // "Open document" inside a sources view focuses that document here — the
  // split-view stays put, the panel just changes what it shows.
  const handleOpenCitedDocument = (citation: Citation): void => {
    if (citation.documentId) {
      openArtifact({
        kind: 'document',
        id: citation.documentId,
        title: citation.title || 'Untitled document',
      });
    }
  };

  // The composer lives in a different subtree (ChatSurface); reuse the
  // existing 'populate-chat-input' bridge with append semantics so citing
  // never clobbers a draft the user is typing.
  const handleCite = (citation: Citation): void => {
    window.dispatchEvent(
      new CustomEvent('populate-chat-input', {
        detail: { text: `"${citation.title}"`, mode: 'append' },
      })
    );
  };

  const fullPageHref =
    artifact.kind === 'document' ? `/documents/${artifact.id}` : undefined;

  return (
    <>
      {/* Sheet backdrop: below md the panel is modal over the transcript. */}
      {isSheet && (
        <div
          aria-hidden="true"
          onClick={closePanel}
          className="fixed inset-0 z-40 bg-(--nous-erebus)/40 md:hidden"
        />
      )}
      <aside
        ref={sheetRef}
        onKeyDown={handleSheetKeyDown}
        tabIndex={isSheet ? -1 : undefined}
        role={isSheet ? 'dialog' : 'region'}
        aria-modal={isSheet ? true : undefined}
        aria-label="Artifact viewer"
        className={cn(
          'flex flex-col bg-(--nous-bg-1)',
          // Tablet and up: docked column in the layout's right slot.
          'md:static md:h-full md:w-[min(40vw,560px)] md:shrink-0',
          'md:border-l md:border-(--nous-border-1)',
          'lg:w-[min(45vw,640px)]',
          // Below md: bottom sheet so the transcript stays reachable.
          'max-md:fixed max-md:inset-x-0 max-md:bottom-0 max-md:z-50 max-md:h-[85dvh]',
          'max-md:rounded-t-(--nous-radius-xl) max-md:border-t max-md:border-(--nous-border-1)',
          'max-md:shadow-(--nous-shadow-lg)',
          'max-md:pb-[env(safe-area-inset-bottom)]',
          className
        )}
      >
        {/* Header */}
        <div className="flex items-center gap-2 border-b border-(--nous-border-1) bg-(--nous-bg-2) px-3 py-2.5">
          <span
            className="font-nous-mono shrink-0 rounded-sm border border-(--nous-border-1) bg-(--nous-bg-1) px-1.5 py-0.5 text-[9px] uppercase text-(--nous-fg-3)"
            style={{ letterSpacing: '0.08em' }}
          >
            {KIND_LABEL[artifact.kind]}
          </span>
          <h2
            className="font-nous-ui min-w-0 flex-1 truncate text-sm font-semibold text-(--nous-fg-1)"
            title={artifactTitle(artifact)}
          >
            {artifactTitle(artifact)}
          </h2>
          <div className="flex shrink-0 items-center gap-0.5">
            {fullPageHref && (
              <Link
                href={fullPageHref}
                aria-label="Open full page"
                title="Open full page"
                className="inline-flex h-8 w-8 items-center justify-center rounded-md text-(--nous-fg-3) transition-colors hover:bg-(--nous-aurum) hover:text-(--nous-fg-1) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ExternalLink aria-hidden="true" className="h-4 w-4" />
              </Link>
            )}
            <IconButton
              icon={
                pinned ? (
                  <PinOff className="h-4 w-4" />
                ) : (
                  <Pin className="h-4 w-4" />
                )
              }
              label={
                pinned
                  ? 'Unpin to let the agent change this view'
                  : 'Pin this artifact'
              }
              aria-pressed={pinned}
              onClick={togglePin}
              className={cn(
                'h-8 w-8 hover:bg-(--nous-aurum)',
                pinned
                  ? 'text-(--nous-fg-accent-safe)'
                  : 'text-(--nous-fg-3) hover:text-(--nous-fg-1)'
              )}
            />
            {onToggleRail && (
              <IconButton
                icon={<ListTree className="h-4 w-4" />}
                label={railOpen ? 'Hide context rail' : 'Show context rail'}
                aria-pressed={railOpen}
                onClick={onToggleRail}
                className="h-8 w-8 text-(--nous-fg-3) hover:bg-(--nous-aurum) hover:text-(--nous-fg-1) max-lg:hidden"
              />
            )}
            <IconButton
              icon={<X className="h-4 w-4" />}
              label="Close artifact panel"
              onClick={closePanel}
              className="h-11 w-11 text-(--nous-fg-3) hover:bg-(--nous-aurum) hover:text-(--nous-fg-1)"
            />
          </div>
        </div>

        {/* Body */}
        <div className="nous-scrollbar flex-1 overflow-y-auto">
          {artifact.kind === 'document' && (
            <DocumentArtifactBody documentId={artifact.id} />
          )}
          {artifact.kind === 'external' && (
            <ExternalArtifactBody artifact={artifact} />
          )}
          {artifact.kind === 'citations' && (
            <CitationPanelBody
              citations={artifact.citations}
              activeCitationId={artifact.activeCitationId}
              diagnosticsTraceId={artifact.traceId}
              onCitationClick={handleOpenCitedDocument}
              onCite={handleCite}
            />
          )}
          {artifact.kind === 'note' && <NoteArtifactBody artifact={artifact} />}
          {artifact.kind === 'draft' && (
            <DraftArtifactBody
              artifact={artifact}
              onCitationClick={handleOpenCitedDocument}
            />
          )}
        </div>
      </aside>
    </>
  );
}
