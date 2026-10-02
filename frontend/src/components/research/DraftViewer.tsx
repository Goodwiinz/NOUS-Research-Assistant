'use client';

/**
 * DraftViewer Component
 * Displays literature review draft content with citations
 */

import React, { useCallback, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Download, FileText, Code, History } from 'lucide-react';
import type { Draft } from '@/services/projectService';
import { ToneToolbar } from './ToneToolbar';
import { RewriteDiffView } from './RewriteDiffView';
import { WriterToolbar } from './WriterToolbar';
import { InsertPreview } from './InsertPreview';
import { OutlineDialog } from './OutlineDialog';
import { DraftReleasePanel, ReleaseBadge } from './DraftReleasePanel';
import { PeerReviewPanel, type PassageTarget } from './PeerReviewPanel';
import { useDraftRelease } from '@/hooks/useDraftRelease';
import { useBackendCapabilities } from '@/hooks/useBackendCapabilities';
import type {
  RewriteResponse,
  WriteResponse,
  OutlineSection,
} from '@/types/scispace';

export interface DraftViewerProps {
  draft: Draft;
  /** GOO-307: enables the release badge, stale banner and promote action. */
  projectId?: string;
  versions?: Array<{ version: number; created_at: string }>;
  documentIds?: string[];
  onVersionChange?: (version: number) => void;
  onExport?: (format: 'markdown' | 'latex') => void;
  onTextRewrite?: (original: string, rewritten: string) => void;
}

export const DraftViewer: React.FC<DraftViewerProps> = ({
  draft,
  projectId,
  versions,
  documentIds,
  onVersionChange,
  onExport,
  onTextRewrite,
}) => {
  const contentRef = useRef<HTMLDivElement>(null);
  const [selectedText, setSelectedText] = useState('');
  const [toneToolbarPos, setToneToolbarPos] = useState<{
    top: number;
    left: number;
  } | null>(null);
  const [rewriteResult, setRewriteResult] = useState<RewriteResponse | null>(
    null
  );
  const [writerToolbarPos, setWriterToolbarPos] = useState<{
    top: number;
    left: number;
  } | null>(null);
  const [writeResult, setWriteResult] = useState<WriteResponse | null>(null);
  // Captured at click time: reading contentRef during render is not allowed.
  const [cursorContext, setCursorContext] = useState('');
  const [showOutlineDialog, setShowOutlineDialog] = useState(false);
  // GOO-314: a peer-review "Old text / New text" link opens that version.
  const [passage, setPassage] = useState<PassageTarget | null>(null);
  const capabilities = useBackendCapabilities(Boolean(projectId));
  const release = useDraftRelease(projectId, draft.id, draft.version);

  const handleTextSelect = useCallback((event: React.MouseEvent) => {
    const selection = window.getSelection();
    if (!selection || !contentRef.current) {
      setSelectedText('');
      setToneToolbarPos(null);
      setWriterToolbarPos(null);
      return;
    }

    if (selection.isCollapsed) {
      // Collapsed selection (cursor click) → WriterToolbar
      setSelectedText('');
      setToneToolbarPos(null);

      // Use click position relative to container
      const containerRect = contentRef.current.getBoundingClientRect();
      const surroundingText = contentRef.current.textContent || '';
      if (surroundingText.trim().length > 0) {
        setCursorContext(surroundingText);
        setWriterToolbarPos({
          top: event.clientY - containerRect.top - 44,
          left: event.clientX - containerRect.left,
        });
      }
      return;
    }

    // Text selection with 5+ words → ToneToolbar
    const text = selection.toString().trim();
    if (text.split(/\s+/).length >= 5) {
      const range = selection.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      const containerRect = contentRef.current.getBoundingClientRect();
      setSelectedText(text);
      setToneToolbarPos({
        top: rect.top - containerRect.top - 44,
        left: rect.left - containerRect.left,
      });
      setWriterToolbarPos(null);
    } else {
      setSelectedText('');
      setToneToolbarPos(null);
      setWriterToolbarPos(null);
    }
  }, []);

  const handleToneRewrite = useCallback((result: RewriteResponse) => {
    setRewriteResult(result);
    setToneToolbarPos(null);
  }, []);

  const handleAcceptRewrite = useCallback(
    (rewritten: string) => {
      if (onTextRewrite && rewriteResult) {
        onTextRewrite(rewriteResult.original, rewritten);
      }
      setRewriteResult(null);
      setSelectedText('');
    },
    [onTextRewrite, rewriteResult]
  );

  const handleRejectRewrite = useCallback(() => {
    setRewriteResult(null);
    setSelectedText('');
  }, []);

  const handleWriteInsert = useCallback((result: WriteResponse) => {
    setWriteResult(result);
    setWriterToolbarPos(null);
  }, []);

  const handleAcceptWrite = useCallback(
    (text: string) => {
      if (onTextRewrite) {
        onTextRewrite('', text);
      }
      setWriteResult(null);
    },
    [onTextRewrite]
  );

  const handleInsertOutline = useCallback(
    (sections: OutlineSection[]) => {
      const outlineText = sections
        .map((s) => `## ${s.title}\n\n${s.description}`)
        .join('\n\n');
      if (onTextRewrite) {
        onTextRewrite('', outlineText);
      }
    },
    [onTextRewrite]
  );

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-border">
        <div className="flex items-center gap-3">
          <FileText className="h-5 w-5 text-primary" />
          <div>
            <h3 className="font-semibold text-foreground">{draft.title}</h3>
            <p className="text-xs text-muted-foreground">
              Version {draft.version}
              {projectId && capabilities.draftRelease && (
                <ReleaseBadge
                  status={
                    release.data?.release_status ??
                    draft.release_status ??
                    'candidate'
                  }
                />
              )}{' '}
              • {draft.word_count} words • {draft.citation_count} citations
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Version Selector */}
          {versions && versions.length > 1 && onVersionChange && (
            <div className="flex items-center gap-2">
              <History className="h-4 w-4 text-muted-foreground" />
              <select
                value={draft.version}
                onChange={(e) => onVersionChange(parseInt(e.target.value, 10))}
                className="px-2 py-1 bg-muted border border-border rounded text-xs text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
              >
                {versions.map((v) => (
                  <option key={v.version} value={v.version}>
                    v{v.version} - {new Date(v.created_at).toLocaleDateString()}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Export Buttons */}
          {onExport && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => onExport('markdown')}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Download className="h-3 w-3" />
                MD
              </button>
              <button
                onClick={() => onExport('latex')}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Code className="h-3 w-3" />
                TeX
              </button>
            </div>
          )}
        </div>
      </div>

      {projectId && capabilities.draftRelease && (
        <DraftReleasePanel projectId={projectId} draft={draft} />
      )}
      {projectId && capabilities.draftRelease && (
        <PeerReviewPanel
          projectId={projectId}
          draft={draft}
          onOpenPassage={(target) => {
            setPassage(target);
            if (
              target.version !== undefined &&
              target.version !== draft.version
            ) {
              onVersionChange?.(target.version);
            }
          }}
        />
      )}
      {passage && (
        <p role="status" className="px-4 py-2 border-b border-border text-xs">
          Passage
          {passage.version !== undefined ? ` in v${passage.version}` : ''} (
          {passage.start}–{passage.end}): “{passage.text}”
        </p>
      )}

      {/* Themes */}
      {draft.themes && draft.themes.length > 0 && (
        <div className="px-4 py-2 border-b border-border flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Themes:</span>
          {draft.themes.map((theme, idx) => (
            <span
              key={idx}
              className="px-2 py-0.5 bg-primary/10 text-primary border border-primary/20 text-xs font-mono rounded"
            >
              {theme}
            </span>
          ))}
        </div>
      )}

      {/* Content — rendered with react-markdown (no rehype-raw), which escapes
          any raw HTML in the LLM-generated draft instead of executing it. The
          previous regex-built-HTML + dangerouslySetInnerHTML pipeline (audit
          #9) relied on DOMPurify as its only XSS defense. */}
      <div className="p-6 max-h-[600px] overflow-y-auto">
        <div
          ref={contentRef}
          className="prose prose-sm max-w-none text-muted-foreground relative"
          onMouseUp={handleTextSelect}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkDocBadges]}
            components={{
              h2: ({ children }) => (
                <h2 className="text-lg font-bold text-primary mt-6 mb-3">
                  {children}
                </h2>
              ),
              h3: ({ children }) => (
                <h3 className="text-base font-bold text-primary/80 mt-4 mb-2">
                  {children}
                </h3>
              ),
              p: ({ children }) => <p className="mb-4">{children}</p>,
              a: ({ href, children }) => (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ),
              span: ({ className, children }) =>
                className === 'doc-badge' ? (
                  <span className="inline-flex items-center px-1 py-0.5 rounded bg-primary/10 text-primary text-xs font-mono">
                    {children}
                  </span>
                ) : (
                  <span className={className}>{children}</span>
                ),
            }}
          >
            {draft.content || ''}
          </ReactMarkdown>
        </div>
        {toneToolbarPos && selectedText && !rewriteResult && (
          <ToneToolbar
            selectedText={selectedText}
            position={toneToolbarPos}
            onRewrite={handleToneRewrite}
            onClose={() => {
              setToneToolbarPos(null);
              setSelectedText('');
            }}
          />
        )}
        {writerToolbarPos &&
          !selectedText &&
          !writeResult &&
          !rewriteResult && (
            <WriterToolbar
              cursorContext={cursorContext}
              position={writerToolbarPos}
              documentIds={documentIds}
              onInsert={handleWriteInsert}
              onOutlineRequest={() => {
                setShowOutlineDialog(true);
                setWriterToolbarPos(null);
              }}
              onClose={() => setWriterToolbarPos(null)}
            />
          )}
      </div>

      {/* Rewrite Diff */}
      {rewriteResult && (
        <div className="px-4 pb-4">
          <RewriteDiffView
            original={rewriteResult.original}
            rewritten={rewriteResult.rewritten}
            toneApplied={rewriteResult.tone_applied}
            citationsPreserved={rewriteResult.citations_preserved}
            onAccept={handleAcceptRewrite}
            onReject={handleRejectRewrite}
          />
        </div>
      )}

      {/* Write Insert Preview */}
      {writeResult && (
        <div className="px-4 pb-4">
          <InsertPreview
            generated={writeResult.generated}
            citationsUsed={writeResult.citations_used}
            sectionType={writeResult.section_type}
            confidence={writeResult.confidence}
            onAccept={handleAcceptWrite}
            onEditFirst={handleAcceptWrite}
            onDiscard={() => setWriteResult(null)}
          />
        </div>
      )}

      {/* Outline Dialog */}
      <OutlineDialog
        isOpen={showOutlineDialog}
        onInsertOutline={handleInsertOutline}
        onClose={() => setShowOutlineDialog(false)}
      />

      {/* Footer */}
      <div className="px-4 py-2 border-t border-border text-xs text-muted-foreground">
        Generated {new Date(draft.created_at).toLocaleString()}
        {draft.is_current && (
          <span className="ml-2 px-1.5 py-0.5 bg-primary/10 text-primary rounded">
            Current
          </span>
        )}
      </div>
    </div>
  );
};

// remark plugin: turn literal "[Doc N]" citation markers into a styled, inert
// badge node. Walks the mdast tree directly (no extra deps) and splits text
// nodes; the badge is a real element produced by the plugin, NOT raw HTML from
// the content, so it carries no XSS risk. The "[Doc N]" text is the only
// content-derived part and react-markdown escapes it like any other text.
/* eslint-disable @typescript-eslint/no-explicit-any */
// Non-global on purpose: String.split() still splits on every match and keeps
// the capture group, and a stateless .test() avoids the lastIndex footgun.
const DOC_BADGE_RE = /(\[Doc \d+\])/;

function remarkDocBadges() {
  return (tree: any): void => {
    const walk = (node: any): void => {
      if (!Array.isArray(node.children)) return;
      const next: any[] = [];
      for (const child of node.children) {
        if (
          child.type === 'text' &&
          typeof child.value === 'string' &&
          DOC_BADGE_RE.test(child.value)
        ) {
          for (const part of child.value.split(DOC_BADGE_RE)) {
            if (part === '') continue;
            if (/^\[Doc \d+\]$/.test(part)) {
              next.push({
                type: 'docBadge',
                data: {
                  hName: 'span',
                  hProperties: { className: 'doc-badge' },
                },
                children: [{ type: 'text', value: part }],
              });
            } else {
              next.push({ type: 'text', value: part });
            }
          }
        } else {
          walk(child);
          next.push(child);
        }
      }
      node.children = next;
    };
    walk(tree);
  };
}
/* eslint-enable @typescript-eslint/no-explicit-any */

export default DraftViewer;
