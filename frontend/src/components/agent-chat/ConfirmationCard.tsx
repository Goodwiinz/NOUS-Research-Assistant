'use client';

import React, { useEffect, useRef, Fragment } from 'react';
import { AlertTriangle, Check, X } from 'lucide-react';

interface ConfirmationAction {
  name: string;
  args: Record<string, unknown>;
}

interface ConfirmationCardProps {
  tools: ConfirmationAction[];
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  isLoading?: boolean;
}

const TOOL_LABELS: Record<string, string> = {
  ingest_arxiv_papers: 'Ingest ArXiv Papers',
  add_document_to_project: 'Add Document to Project',
  create_project_note: 'Create Project Note',
  create_draft: 'Generate Draft',
  revise_draft: 'Revise Draft',
  create_project: 'Create Project',
  search_external_database: 'Search External Database',
  delete_document: 'Delete Document',
  update_project: 'Update Project',
  remove_document_from_project: 'Remove Document from Project',
};

/**
 * Format a single arg value for display.
 * Mirrors the formatArgValue helper in /chat/page.tsx.
 * Values truncated to 80 chars; objects/arrays rendered as compact JSON.
 */
function formatArgValue(value: unknown, max = 80): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') {
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  try {
    const s = JSON.stringify(value);
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  } catch {
    return '[unserializable]';
  }
}

export function ConfirmationCard({
  tools,
  message,
  onConfirm,
  onCancel,
  isLoading,
}: ConfirmationCardProps) {
  const approveRef = useRef<HTMLButtonElement>(null);

  // Focus the Approve button when the card mounts so keyboard users can act immediately.
  useEffect(() => {
    approveRef.current?.focus();
  }, []);

  return (
    <div
      role="alertdialog"
      aria-label="Approval needed"
      aria-describedby="fab-hitl-desc"
      className="rounded-(--nous-radius-md) border border-(--nous-border-1) bg-(--nous-bg-2) text-sm my-2 overflow-hidden outline-hidden"
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !isLoading) {
          // Without this the same keypress also reaches the panel's own
          // Escape handler and closes the whole chat.
          e.stopPropagation();
          onCancel();
        }
      }}
      tabIndex={-1}
    >
      {/* Header row */}
      <div className="flex items-start gap-2 px-3 py-2.5">
        <AlertTriangle
          aria-hidden
          className="h-4 w-4 shrink-0 mt-0.5"
          style={{ color: 'var(--nous-corona)' }}
        />
        <div className="flex-1 min-w-0">
          <p
            id="fab-hitl-desc"
            className="font-medium text-xs"
            style={{
              color: 'var(--nous-fg-1)',
              fontFamily: 'var(--nous-font-ui)',
            }}
          >
            {message}
          </p>

          {/* Per-tool: label pill + args table */}
          <ul className="mt-2 space-y-2.5">
            {tools.map((tool, i) => {
              const label = TOOL_LABELS[tool.name] ?? tool.name;
              const argEntries = Object.entries(tool.args).filter(
                ([, v]) => v !== null && v !== undefined && v !== ''
              );

              return (
                <li key={i}>
                  {/* Tool name pill */}
                  <div className="flex items-center gap-1.5 mb-1">
                    <span
                      className="w-1 h-1 rounded-full shrink-0"
                      style={{ background: 'var(--nous-corona)' }}
                    />
                    <span
                      className="text-xs font-medium"
                      style={{
                        color: 'var(--nous-fg-1)',
                        fontFamily: 'var(--nous-font-ui)',
                      }}
                    >
                      {label}
                    </span>
                    {label !== tool.name && (
                      <span
                        className="rounded px-1 py-0.5 text-[10px] leading-none"
                        style={{
                          color: 'var(--nous-fg-3)',
                          fontFamily: 'var(--nous-font-mono)',
                          background: 'var(--nous-bg-1)',
                        }}
                      >
                        {tool.name}
                      </span>
                    )}
                  </div>

                  {/* Args table */}
                  {argEntries.length > 0 && (
                    <dl
                      className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-lg p-2 text-[11px] border"
                      style={{
                        background: 'var(--nous-bg-1)',
                        borderColor: 'var(--nous-border-1)',
                      }}
                      aria-label={`Arguments for ${label}`}
                    >
                      {argEntries.map(([key, value]) => (
                        <Fragment key={key}>
                          <dt
                            className="whitespace-nowrap"
                            style={{
                              color: 'var(--nous-fg-3)',
                              fontFamily: 'var(--nous-font-ui)',
                            }}
                          >
                            {key}
                          </dt>
                          <dd
                            className="break-all"
                            style={{
                              color: 'var(--nous-fg-2)',
                              fontFamily: 'var(--nous-font-mono)',
                            }}
                          >
                            {formatArgValue(value)}
                          </dd>
                        </Fragment>
                      ))}
                    </dl>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      </div>

      {/* Action row */}
      <div
        className="flex items-center gap-2 px-3 py-2 border-t"
        style={{ borderColor: 'var(--nous-border-1)' }}
      >
        <button
          ref={approveRef}
          onClick={onConfirm}
          disabled={isLoading}
          aria-label="Approve and run the requested action"
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg disabled:opacity-50 transition-all duration-150 ease-out focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
          style={{
            background: 'var(--nous-sol)',
            color: 'var(--nous-erebus, #0A0A0E)',
            fontFamily: 'var(--nous-font-ui)',
          }}
        >
          <Check className="h-3 w-3" aria-hidden />
          {isLoading ? 'Processing…' : 'Approve and run'}
        </button>
        <button
          onClick={onCancel}
          disabled={isLoading}
          aria-label="Cancel — do not run the action"
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border disabled:opacity-50 transition-all duration-150 ease-out focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
          style={{
            borderColor: 'var(--nous-mars, #ef4444)',
            background:
              'color-mix(in srgb, var(--nous-mars, #ef4444) 5%, transparent)',
            color: 'var(--nous-mars, #ef4444)',
            fontFamily: 'var(--nous-font-ui)',
          }}
        >
          <X className="h-3 w-3" aria-hidden />
          Cancel
        </button>
      </div>
    </div>
  );
}
