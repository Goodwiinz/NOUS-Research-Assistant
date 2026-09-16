'use client';

import { IconButton } from '@/components/ui/icon-button';
import {
  downloadFile,
  exportAsJson,
  exportAsMarkdown,
} from '@/components/chat/shared/exportConversation';
import { exportThread, type ExportFormat } from '@/services/export-service';
import {
  ClipboardCopy,
  Download,
  FileJson,
  FileText,
  Loader2,
  Menu,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { memo, useState } from 'react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { JobsIndicator } from './JobsIndicator';

export const EXPORT_BLOCKED_REASON =
  'Export is available when the response finishes';

interface ExportableMessage {
  role: string;
  content: string;
  timestamp: number;
}

interface ChatHeaderProps {
  messages?: ExportableMessage[];
  chatTitle?: string;
  /** Active thread id — when present, exports use the rich backend
   * thread-export endpoint (with citations + provenance). Absent for a brand-
   * new chat, where the local in-memory fallback is used instead. */
  threadId?: string | null;
  /** True while a response is streaming into this thread. The backend export
   * serves a persisted snapshot that cannot contain the in-flight turn, so the
   * export actions are disabled rather than silently omitting it. */
  isStreaming?: boolean;
  onCopyAll?: () => void;
  onMobileSidebarToggle?: () => void;
}

export const ChatHeader = memo(function ChatHeader({
  messages = [],
  chatTitle = 'Chat',
  threadId,
  isStreaming = false,
  onCopyAll,
  onMobileSidebarToggle,
}: ChatHeaderProps) {
  const [exportOpen, setExportOpen] = useState(false);
  const [isExporting, setIsExporting] = useState(false);

  const slug = chatTitle.toLowerCase().replace(/\s+/g, '-').slice(0, 40);

  // When a thread is persisted, delegate to the backend export — it carries
  // citations, tool executions, token usage and provenance the local
  // serializer drops. The endpoint is POST with format/options as Query params.
  // For a new chat with no thread yet, fall back to the local in-memory export.
  const runExport = async (format: ExportFormat): Promise<void> => {
    setExportOpen(false);
    if (isStreaming) return;
    if (threadId) {
      setIsExporting(true);
      try {
        await exportThread(threadId, format, {
          includeCitations: true,
          includeMetadata: true,
        });
      } catch {
        toast.error('Export failed. Please try again.');
      } finally {
        setIsExporting(false);
      }
      return;
    }
    if (format === 'pdf' || format === 'html') return; // local fallback: MD/JSON only
    if (format === 'json') {
      downloadFile(
        `${slug}.json`,
        'application/json',
        exportAsJson(chatTitle, messages)
      );
    } else {
      downloadFile(
        `${slug}.md`,
        'text/markdown',
        exportAsMarkdown(chatTitle, messages)
      );
    }
  };

  return (
    <div className="bg-(--nous-bg-1) flex h-12 sm:h-14 shrink-0 items-center gap-2 sm:gap-3 border-b border-(--nous-border-1) px-3 sm:px-4 z-40">
      {/* Left: sidebar trigger + title */}
      <div className="flex items-center gap-2 sm:gap-3 min-w-0 flex-1">
        {onMobileSidebarToggle && (
          <button
            type="button"
            onClick={onMobileSidebarToggle}
            className="xl:hidden h-11 w-11 shrink-0 flex items-center justify-center text-(--nous-fg-2) hover:text-(--nous-sol) hover:bg-(--nous-sol)/8 transition-colors rounded-lg"
            aria-label="Toggle chat history"
          >
            <Menu className="w-5 h-5" />
          </button>
        )}

        {/* Conversation title — visible at all breakpoints */}
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="text-[13px] sm:text-sm font-medium text-(--nous-fg-1) truncate"
            style={{ fontFamily: 'var(--nous-font-ui)' }}
            title={chatTitle}
          >
            {chatTitle}
          </span>
        </div>
      </div>

      {/* Right: chat actions */}
      <div className="flex items-center gap-1.5 shrink-0 ml-auto">
        {/* Background work (uploads, ingests, extraction) — renders nothing
            until there is a job to report. */}
        <JobsIndicator />

        {messages.length > 0 && onCopyAll && (
          <IconButton
            label="Copy all messages"
            icon={<ClipboardCopy className="w-4 h-4" />}
            className="p-1.5 text-(--nous-fg-3) hover:text-(--nous-fg-1) hover:bg-(--nous-sol)/8 rounded-lg transition-colors"
            onClick={onCopyAll}
          />
        )}

        {messages.length > 0 && (
          <DropdownMenu open={exportOpen} onOpenChange={setExportOpen}>
            <DropdownMenuTrigger asChild disabled={isStreaming}>
              <IconButton
                label="Export chat"
                showTooltip={false}
                icon={
                  isExporting ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Download className="w-4 h-4" />
                  )
                }
                className="p-1.5 text-(--nous-fg-3) hover:text-(--nous-fg-1) hover:bg-(--nous-sol)/8 rounded-lg transition-colors"
                disabled={isStreaming}
                aria-disabled={isStreaming}
                title={isStreaming ? EXPORT_BLOCKED_REASON : undefined}
              />
            </DropdownMenuTrigger>
            <span className="sr-only" role="status" aria-live="polite">
              {isStreaming
                ? EXPORT_BLOCKED_REASON
                : isExporting
                  ? 'Preparing export…'
                  : ''}
            </span>
            <DropdownMenuContent
              align="end"
              className="mt-1 w-48 rounded-xl border border-(--nous-border-1) bg-(--nous-bg-2) shadow-lg z-50 overflow-hidden p-1"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              <DropdownMenuItem asChild disabled={isExporting || isStreaming}>
                <button
                  onClick={() => runExport('markdown')}
                  disabled={isExporting || isStreaming}
                  aria-disabled={isExporting || isStreaming}
                  title={isStreaming ? EXPORT_BLOCKED_REASON : undefined}
                  className="w-full flex items-center gap-2 px-3 py-2 text-[12px] text-(--nous-fg-2) hover:bg-(--nous-sol)/8 hover:text-(--nous-fg-1) focus-visible:bg-(--nous-sol)/8 focus-visible:outline-hidden rounded-lg transition-colors disabled:opacity-50"
                >
                  <FileText className="w-3.5 h-3.5 text-(--nous-fg-3)" />
                  Export as Markdown
                </button>
              </DropdownMenuItem>
              <DropdownMenuItem
                asChild
                disabled={isExporting || isStreaming || !threadId}
              >
                <button
                  onClick={() => runExport('pdf')}
                  disabled={isExporting || isStreaming || !threadId}
                  aria-disabled={isExporting || isStreaming || !threadId}
                  title={isStreaming ? EXPORT_BLOCKED_REASON : undefined}
                  className="w-full flex items-center gap-2 px-3 py-2 text-[12px] text-(--nous-fg-2) hover:bg-(--nous-sol)/8 hover:text-(--nous-fg-1) focus-visible:bg-(--nous-sol)/8 focus-visible:outline-hidden rounded-lg transition-colors disabled:opacity-50"
                >
                  <FileText className="w-3.5 h-3.5 text-(--nous-fg-3)" />
                  Export as PDF
                </button>
              </DropdownMenuItem>
              <DropdownMenuItem asChild disabled={isExporting || isStreaming}>
                <button
                  onClick={() => runExport('json')}
                  disabled={isExporting || isStreaming}
                  aria-disabled={isExporting || isStreaming}
                  title={isStreaming ? EXPORT_BLOCKED_REASON : undefined}
                  className="w-full flex items-center gap-2 px-3 py-2 text-[12px] text-(--nous-fg-2) hover:bg-(--nous-sol)/8 hover:text-(--nous-fg-1) focus-visible:bg-(--nous-sol)/8 focus-visible:outline-hidden rounded-lg transition-colors disabled:opacity-50"
                >
                  <FileJson className="w-3.5 h-3.5 text-(--nous-fg-3)" />
                  Export as JSON
                </button>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </div>
  );
});
