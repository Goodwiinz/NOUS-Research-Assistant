'use client';

import React, { useLayoutEffect, useRef } from 'react';
import { DocumentInlineViewer } from './DocumentInlineViewer';
import { useRouter } from 'next/navigation';
import { stagePdfQuoteIntent } from '@/lib/pdf/chat-intent';
import { captureChatSession, useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';

interface DocumentPreviewTabProps {
  documentId: string;
  filename: string;
  /** Frontend `file_type` or backend `document_type`, whichever is present. */
  kind?: string;
  mimeType?: string;
}

/**
 * The document detail page's Preview tab: the shared inline viewer plus the
 * tab's vertical rhythm. All viewer behavior lives in DocumentInlineViewer,
 * which the chat artifact panel reuses without this spacing.
 */
export function DocumentPreviewTab(props: DocumentPreviewTabProps) {
  const router = useRouter();
  const lifetime = useRef<object | null>(null);
  useLayoutEffect(() => {
    lifetime.current = {};
    return () => {
      lifetime.current = null;
    };
  }, [props.documentId]);
  return (
    <div className="py-6">
      <DocumentInlineViewer
        {...props}
        onAskAboutPdf={async (range, quote, quoteRange) => {
          const capturedLifetime = lifetime.current;
          const isCurrentSession = captureChatSession();
          const actor = useAuthStore.getState().user;
          if (
            !capturedLifetime ||
            !actor ||
            !useAuthStore.getState().isAuthenticated
          )
            return false;
          // The chat route bootstraps this same destination. Finish it before
          // binding a quote to workspace ownership, including first use.
          await useChatStore.getState().initializeDefaultWorkspace();
          const auth = useAuthStore.getState();
          if (
            lifetime.current !== capturedLifetime ||
            !isCurrentSession() ||
            !auth.isAuthenticated ||
            auth.user?.id !== actor.id ||
            auth.user?.organization_id !== actor.organization_id
          )
            return false;
          const destination = useChatStore.getState();
          if (
            !destination.currentWorkspaceId ||
            destination.isLoadingConversations ||
            destination.error
          )
            throw new Error('PDF chat workspace is unavailable');
          const params = new URLSearchParams({
            new: '1',
            pdfDocumentId: props.documentId,
          });
          if (range) {
            params.set('pdfStartIndex', String(range[0]));
            params.set('pdfEndIndex', String(range[1]));
          }
          const token = stagePdfQuoteIntent(
            props.documentId,
            quoteRange,
            quote
          );
          if (quote && !token)
            throw new Error('PDF selection handoff is unavailable');
          if (token && quoteRange) {
            params.set('pdfQuoteIntent', token);
            params.set('pdfQuoteStartIndex', String(quoteRange[0]));
            params.set('pdfQuoteEndIndex', String(quoteRange[1]));
          }
          router.push(`/chat?${params.toString()}`);
          return true;
        }}
      />
    </div>
  );
}
