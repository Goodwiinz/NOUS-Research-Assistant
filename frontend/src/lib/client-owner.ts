/**
 * Owner stamp for the user-scoped client caches that survive a page load
 * (`chat-storage`, `default-workspace-*`, ...).
 *
 * The auth store only knows the current account in memory, and memory is gone
 * on every load. Without a persisted stamp each load therefore looks like an
 * account switch and wipes the caches even when the same account is coming
 * back. This stamp records who the caches belong to so a same-user reload
 * keeps them.
 *
 * It is a hint, not a credential: a missing or unreadable stamp reads as
 * "unknown owner", and the auth store treats unknown as a different account
 * and clears (fail closed). That keeps the GOO-350 guarantee that account B
 * never sees account A's cached client data, across reloads, sign-out /
 * sign-in, and tabs. Nothing is ever restored from this value.
 */

export const CLIENT_OWNER_STORAGE_KEY = 'nous:client-owner:v1';

function ownerStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    // Some browsers expose the property but throw SecurityError while
    // acquiring it under restricted storage policies.
    return null;
  }
}

/** The user id the persisted client caches belong to, or null when unknown. */
export function readClientOwner(): string | null {
  const storage = ownerStorage();
  if (!storage) return null;
  try {
    const value = storage.getItem(CLIENT_OWNER_STORAGE_KEY);
    return typeof value === 'string' && value.length > 0 ? value : null;
  } catch {
    // Unreadable is unknown, and unknown clears. Never guess an owner.
    return null;
  }
}

export function writeClientOwner(userId: string): void {
  const storage = ownerStorage();
  if (!storage || !userId) return;
  try {
    storage.setItem(CLIENT_OWNER_STORAGE_KEY, userId);
  } catch {
    // A failed write leaves the stamp absent or stale. Either reads as a
    // different owner on the next load, so the caches are cleared again
    // rather than trusted.
  }
}

export function clearClientOwner(): void {
  const storage = ownerStorage();
  if (!storage) return;
  try {
    storage.removeItem(CLIENT_OWNER_STORAGE_KEY);
  } catch {
    // Best effort: a stale stamp can only ever cause an extra clear.
  }
}
