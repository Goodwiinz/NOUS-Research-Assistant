'use client';

import {
  createContext,
  useContext,
  useCallback,
  useMemo,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type ReactElement,
  type ReactNode,
  type SetStateAction,
} from 'react';
import { useArtifactScope } from '@/hooks/chat/useArtifactScope';
import { usePdfChatBinding } from '@/hooks/chat/usePdfChatBinding';
import type { PdfChatContext } from '@/types/pdf-chat';
import {
  useArtifactPanelStore,
  type Artifact,
} from '@/store/artifactPanelStore';

interface ChatRagContextValue {
  ragEnabled: boolean;
  setRagEnabled: Dispatch<SetStateAction<boolean>>;
  pdfContext: PdfChatContext | null;
  setPdfContext: (context: PdfChatContext | null) => void;
  consumePdfSelection: (expected: PdfChatContext) => void;
  pdfIntentPending: boolean;
  pdfIsBound: boolean;
  pdfBindingError: string | null;
  retryPdfBinding: () => void;
  preparePdfThreadAdoption: (
    threadId: string,
    projectId: string | null
  ) => void;
}

const defaultValue: ChatRagContextValue = {
  ragEnabled: true,
  setRagEnabled: () => undefined,
  pdfContext: null,
  setPdfContext: () => undefined,
  consumePdfSelection: () => undefined,
  pdfIntentPending: false,
  pdfIsBound: false,
  pdfBindingError: null,
  retryPdfBinding: () => undefined,
  preparePdfThreadAdoption: () => undefined,
};

const ChatRagContext = createContext<ChatRagContextValue>(defaultValue);

export function ChatRagProvider({
  children,
  pdfIntentPending = false,
}: {
  children: ReactNode;
  pdfIntentPending?: boolean;
}): ReactElement {
  const [ragEnabled, setRagEnabled] = useState(true);
  const scope = useArtifactScope();
  const binding = usePdfChatBinding(scope);
  const scopeRef = useRef(scope);
  useLayoutEffect(() => {
    scopeRef.current = scope;
  }, [scope]);
  const [pdf, setPdf] = useState<{
    scope: string | null;
    context: PdfChatContext | null;
  }>({ scope, context: null });
  const [adoption, setAdoption] = useState<{
    from: string;
    to: string;
    context: PdfChatContext;
    artifact: Artifact | null;
  } | null>(null);
  const receipt = adoption;
  const adoptedContext =
    receipt && receipt.from === pdf.scope && receipt.to === scope
      ? receipt.context
      : null;
  // Synchronous scope gating prevents an old account/thread selection from
  // being painted or submitted during the effect-cleanup window.
  if (pdf.scope !== scope) {
    setPdf({ scope, context: adoptedContext });
    if (adoption && scope !== adoption.to) setAdoption(null);
  }
  const transient = pdf.scope === scope ? pdf.context : adoptedContext;
  const pdfContext = useMemo(() => {
    if (!binding.bound) return transient;
    if (!binding.document)
      return !binding.error && transient?.documentId === binding.documentId
        ? transient
        : null;
    if (transient?.documentId === binding.document.id) return transient;
    return {
      documentId: binding.document.id,
      title: binding.document.title || binding.document.filename,
    };
  }, [
    binding.bound,
    binding.document,
    binding.documentId,
    binding.error,
    transient,
  ]);
  const setPdfContext = useCallback(
    (context: PdfChatContext | null) => {
      if (
        scope &&
        scope === scopeRef.current &&
        (!binding.bound || context?.documentId === binding.document?.id)
      ) {
        setAdoption(null);
        setPdf({ scope, context });
      }
    },
    [scope, binding.bound, binding.document?.id]
  );
  const consumePdfSelection = useCallback((expected: PdfChatContext) => {
    setPdf((current) => {
      if (current.scope !== scopeRef.current || current.context !== expected)
        return current;
      return {
        ...current,
        context: {
          documentId: expected.documentId,
          title: expected.title,
          ...(expected.selectionPageRange
            ? { selectionPageRange: expected.selectionPageRange }
            : {}),
        },
      };
    });
    // A completed send must not leave an adoption receipt that can restore it.
    setAdoption((current) => (current?.context === expected ? null : current));
  }, []);
  const preparePdfThreadAdoption = useCallback(
    (threadId: string, projectId: string | null) => {
      if (!scope || scope !== scopeRef.current || !pdfContext) return;
      const identity: unknown = JSON.parse(scope);
      if (
        !Array.isArray(identity) ||
        identity.length !== 5 ||
        identity[4] !== null
      )
        return;
      // This receipt is minted only after createThread returns and before its
      // caller selects that exact new thread. Ordinary navigation cannot mint it.
      const to = JSON.stringify([...identity.slice(0, 3), projectId, threadId]);
      const artifact = useArtifactPanelStore.getState().artifact;
      setAdoption({
        from: scope,
        to,
        context: pdfContext,
        artifact:
          artifact?.kind === 'document' && artifact.id === pdfContext.documentId
            ? artifact
            : null,
      });
    },
    [scope, pdfContext]
  );
  useLayoutEffect(() => {
    const pending = adoption;
    if (!pending || scope === pending.from) return;
    if (scope !== pending.to || !pending.artifact) return;
    // The layout has reset the panel to the new scope. Carry only this PDF,
    // never other tabs or state from a previously visited thread.
    const panel = useArtifactPanelStore.getState();
    panel.setScope(scope);
    panel.openArtifact(pending.artifact, { scope });
  }, [scope, adoption]);
  const restoredBinding = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (
      !scope ||
      !binding.bound ||
      binding.pending ||
      !binding.document ||
      restoredBinding.current === scope
    )
      return;
    restoredBinding.current = scope;
    const panel = useArtifactPanelStore.getState();
    if (!panel.artifact) {
      panel.openArtifact(
        {
          kind: 'document',
          id: binding.document.id,
          title: binding.document.title || binding.document.filename,
        },
        { scope }
      );
    }
  }, [scope, binding.bound, binding.pending, binding.document]);
  const value = useMemo(
    () => ({
      ragEnabled,
      setRagEnabled,
      pdfContext,
      setPdfContext,
      consumePdfSelection,
      pdfIntentPending: pdfIntentPending || binding.pending,
      pdfIsBound: binding.bound,
      pdfBindingError: binding.error,
      retryPdfBinding: binding.retry,
      preparePdfThreadAdoption,
    }),
    [
      ragEnabled,
      pdfContext,
      setPdfContext,
      consumePdfSelection,
      pdfIntentPending,
      binding.pending,
      binding.bound,
      binding.error,
      binding.retry,
      preparePdfThreadAdoption,
    ]
  );

  return (
    <ChatRagContext.Provider value={value}>{children}</ChatRagContext.Provider>
  );
}

export function useChatRag(): ChatRagContextValue {
  return useContext(ChatRagContext);
}
