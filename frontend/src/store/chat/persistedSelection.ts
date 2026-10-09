/**
 * Ownership of the persisted chat selection (`chat-storage`).
 *
 * The persisted selection is written for one account and must only ever be
 * adopted by that account. Hydration happens at module load, before the auth
 * store knows who the user is, so the persist middleware does not merge the
 * selection into state. It is parked here instead, and `selectionSlice`'s
 * `adoptPersistedSelection` applies it once the verified user is published,
 * only when the owners match. A payload without an owner (what older builds
 * wrote) is never adopted. The parked record is consumed on the first
 * adoption attempt, whatever its outcome, so a later account in the same tab
 * cannot pick it up either.
 */
import type { ChatStore } from './types';

export interface PersistedChatSelection {
  ownerUserId: string;
  currentWorkspaceId: string | null;
  currentConversationId: string | null;
  currentThreadId: string | null;
}

let pendingSelection: PersistedChatSelection | null = null;

function nullableId(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function toPersistedSelection(value: unknown): PersistedChatSelection | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  const ownerUserId = nullableId(record.ownerUserId);
  if (!ownerUserId) return null;
  return {
    ownerUserId,
    currentWorkspaceId: nullableId(record.currentWorkspaceId),
    currentConversationId: nullableId(record.currentConversationId),
    currentThreadId: nullableId(record.currentThreadId),
  };
}

/**
 * `persist` merge: park the owned selection for a later adoption and keep
 * only the account-neutral UI preference eagerly.
 */
export function mergePersistedChatState(
  persisted: unknown,
  current: ChatStore
): ChatStore {
  pendingSelection = toPersistedSelection(persisted);
  const sidebarCollapsed = (persisted as { sidebarCollapsed?: unknown } | null)
    ?.sidebarCollapsed;
  return typeof sidebarCollapsed === 'boolean'
    ? { ...current, sidebarCollapsed }
    : current;
}

/** Hand over the parked selection; it is gone after this call. */
export function takePersistedSelection(): PersistedChatSelection | null {
  const selection = pendingSelection;
  pendingSelection = null;
  return selection;
}

/**
 * A store reset ends the account's client state, parked hydration included:
 * the auth store resets when it cannot vouch for the origin's caches, and the
 * account published afterwards must not inherit a selection that reset threw
 * away merely because the payload named it.
 */
export function discardPersistedSelection(): void {
  pendingSelection = null;
}
