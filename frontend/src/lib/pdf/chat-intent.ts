import { onChatSessionReset, useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import type { PdfPageRange } from '@/types/pdf-chat';

// One transient navigation receipt, never a persisted entity or browser URL value.
let pending: {
  token: string;
  owner: string;
  documentId: string;
  range: PdfPageRange;
  quote: string;
} | null = null;

const owner = (): string | null => {
  const auth = useAuthStore.getState();
  return auth.isAuthenticated && auth.user?.id && auth.user.organization_id
    ? JSON.stringify([
        auth.user.id,
        auth.user.organization_id,
        useChatStore.getState().currentWorkspaceId,
      ])
    : null;
};

let watchingOwner = false;
function watchOwner(): void {
  if (watchingOwner) return;
  watchingOwner = true;
  onChatSessionReset(() => {
    pending = null;
  });
  const discardForeignIntent = (): void => {
    if (pending && pending.owner !== owner()) pending = null;
  };
  // Re-entering a previous workspace/account must not revive its old URL token.
  useChatStore.subscribe(discardForeignIntent);
  useAuthStore.subscribe(discardForeignIntent);
}

export function stagePdfQuoteIntent(
  documentId: string,
  range?: PdfPageRange,
  quote?: string
): string | undefined {
  watchOwner();
  pending = null;
  const identity = owner();
  if (!identity || !range || !quote || quote.length > 2002) return;
  const token = crypto.randomUUID();
  pending = { token, owner: identity, documentId, range: [...range], quote };
  return token;
}

export function consumePdfQuoteIntent(
  token: string | null,
  documentId: string,
  range?: PdfPageRange
): string | undefined {
  const receipt = pending;
  if (!receipt || receipt.token !== token) return;
  pending = null;
  if (
    receipt.owner !== owner() ||
    receipt.documentId !== documentId ||
    !range ||
    receipt.range[0] !== range[0] ||
    receipt.range[1] !== range[1]
  )
    return;
  return receipt.quote;
}
