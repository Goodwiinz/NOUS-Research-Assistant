'use client';

import { useCallback, useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useRouter, useSearchParams } from 'next/navigation';
import { useChatRag } from '@/components/chat/ChatRagContext';
import { useArtifactScope } from './useArtifactScope';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { documentService } from '@/services/documentService';
import type { PdfPageRange } from '@/types/pdf-chat';
import { consumePdfQuoteIntent } from '@/lib/pdf/chat-intent';

function validIndices(first: string | null, last: string | null): boolean {
  if (first === null && last === null) return true;
  const start = Number(first);
  const end = Number(last);
  return (
    first !== null &&
    last !== null &&
    /^\d+$/.test(first) &&
    /^\d+$/.test(last) &&
    Number.isSafeInteger(start) &&
    Number.isSafeInteger(end) &&
    start >= 0 &&
    end >= start &&
    end - start < 100
  );
}

/** A document-detail link enters an explicit new chat. Resolve its document
 * through the existing query owner before adopting any composer intent. */
export function usePdfChatRoute(readyForNewChat: boolean): {
  error: string | null;
  dismiss: () => void;
} {
  const params = useSearchParams();
  const router = useRouter();
  const scope = useArtifactScope();
  const { setPdfContext } = useChatRag();
  const applied = useRef<string | null>(null);
  const documentId = params.get('pdfDocumentId');
  const validId = Boolean(
    documentId &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      documentId
    )
  );
  const first = params.get('pdfStartIndex');
  const last = params.get('pdfEndIndex');
  const quoteToken = params.get('pdfQuoteIntent');
  const quoteFirst = params.get('pdfQuoteStartIndex');
  const quoteLast = params.get('pdfQuoteEndIndex');
  const hasRange = first !== null || last !== null;
  const start = Number(first);
  const end = Number(last);
  const validRange =
    validIndices(first, last) && validIndices(quoteFirst, quoteLast);
  const { data, isError } = useQuery({
    queryKey: ['document', documentId],
    queryFn: () => documentService.getDocument(documentId!),
    enabled: Boolean(readyForNewChat && scope && validId && validRange),
  });
  const isPdf =
    data &&
    ((data.file_type || data.document_type || '').toLowerCase() === 'pdf' ||
      data.mime_type === 'application/pdf');
  const intent = `${scope}:${documentId}:${first}:${last}:${quoteToken}:${quoteFirst}:${quoteLast}`;
  const consumeIntent = useCallback(() => {
    const next = new URLSearchParams(params.toString());
    [
      'pdfDocumentId',
      'pdfStartIndex',
      'pdfEndIndex',
      'pdfQuoteIntent',
      'pdfQuoteStartIndex',
      'pdfQuoteEndIndex',
    ].forEach((key) => next.delete(key));
    router.replace(`/chat?${next.toString()}`, { scroll: false });
  }, [params, router]);
  const dismiss = useCallback(() => {
    // Fence a lookup that settles before the router publishes its new URL.
    applied.current = intent;
    consumePdfQuoteIntent(quoteToken, documentId ?? '');
    setPdfContext(null);
    consumeIntent();
  }, [intent, setPdfContext, consumeIntent, quoteToken, documentId]);
  useEffect(() => {
    if (
      !readyForNewChat ||
      !scope ||
      !validId ||
      !validRange ||
      !data ||
      !isPdf ||
      applied.current === intent
    )
      return;
    applied.current = intent;
    const selectionPageRange: PdfPageRange | undefined = hasRange
      ? [start, end]
      : undefined;
    const selectionQuoteRange: PdfPageRange | undefined =
      quoteFirst !== null && quoteLast !== null
        ? [Number(quoteFirst), Number(quoteLast)]
        : undefined;
    const selectionQuote = consumePdfQuoteIntent(
      quoteToken,
      data.id,
      selectionQuoteRange
    );
    setPdfContext({
      documentId: data.id,
      title: data.title || data.filename,
      ...(selectionPageRange ? { selectionPageRange } : {}),
      ...(selectionQuote ? { selectionQuote, selectionQuoteRange } : {}),
    });
    const targetRange = selectionQuote
      ? selectionQuoteRange
      : selectionPageRange;
    useArtifactPanelStore.getState().openArtifact(
      {
        kind: 'document',
        id: data.id,
        title: data.title || data.filename,
        ...(targetRange ? { targetPage: targetRange[0] + 1 } : {}),
      },
      { scope }
    );
    consumeIntent();
  }, [
    readyForNewChat,
    scope,
    validId,
    validRange,
    data,
    isPdf,
    intent,
    hasRange,
    start,
    end,
    quoteToken,
    quoteFirst,
    quoteLast,
    setPdfContext,
    consumeIntent,
  ]);

  let error: string | null = null;
  if (!validId || !validRange)
    error = 'This PDF chat link has an invalid document or page range.';
  if (isError || (data && !isPdf))
    error =
      'This PDF is unavailable for chat. Open an accessible PDF and try again.';
  return { error: documentId === null ? null : error, dismiss };
}
