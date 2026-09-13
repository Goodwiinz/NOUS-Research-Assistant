'use client';

export const CHAT_AUTH_RECOVERY_STORAGE_KEY = 'nous:chat-auth-recovery:v1';

const CHAT_AUTH_RECOVERY_TTL_MS = 15 * 60 * 1_000;

interface ChatAuthRecoveryRecord {
  version: 1;
  attemptId: string;
  ownerUserId: string;
  threadId: string;
  prompt: string;
  expiresAt: number;
  state: 'armed' | 'ready';
}

export interface StageChatAuthRecoveryInput {
  attemptId: string;
  ownerUserId: string;
  threadId: string;
  prompt: string;
}

function recoveryStorage(): Storage | null {
  return typeof window === 'undefined' ? null : window.sessionStorage;
}

function removeRecoveryRecord(storage: Storage): void {
  try {
    storage.removeItem(CHAT_AUTH_RECOVERY_STORAGE_KEY);
  } catch {
    // Storage can be denied in private/restricted browsing. Recovery is
    // best-effort and must never block the sign-in route.
  }
}

function isRecoveryRecord(value: unknown): value is ChatAuthRecoveryRecord {
  if (!value || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  return (
    record.version === 1 &&
    typeof record.attemptId === 'string' &&
    record.attemptId.length > 0 &&
    typeof record.ownerUserId === 'string' &&
    record.ownerUserId.length > 0 &&
    typeof record.threadId === 'string' &&
    record.threadId.length > 0 &&
    typeof record.prompt === 'string' &&
    record.prompt.length > 0 &&
    typeof record.expiresAt === 'number' &&
    Number.isFinite(record.expiresAt) &&
    (record.state === 'armed' || record.state === 'ready')
  );
}

function readRecoveryRecord(
  storage: Storage,
  now: number
): ChatAuthRecoveryRecord | null {
  try {
    const raw = storage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isRecoveryRecord(parsed) || parsed.expiresAt <= now) {
      removeRecoveryRecord(storage);
      return null;
    }
    return parsed;
  } catch {
    removeRecoveryRecord(storage);
    return null;
  }
}

export function stageChatAuthRecovery(
  input: StageChatAuthRecoveryInput,
  now = Date.now()
): boolean {
  const storage = recoveryStorage();
  const prompt = input.prompt.trim();
  if (
    !storage ||
    !input.attemptId ||
    !input.ownerUserId ||
    !input.threadId ||
    !prompt
  ) {
    return false;
  }
  const record: ChatAuthRecoveryRecord = {
    version: 1,
    attemptId: input.attemptId,
    ownerUserId: input.ownerUserId,
    threadId: input.threadId,
    prompt,
    expiresAt: now + CHAT_AUTH_RECOVERY_TTL_MS,
    state: 'armed',
  };
  try {
    storage.setItem(CHAT_AUTH_RECOVERY_STORAGE_KEY, JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

export function markChatAuthRecoveryReady(
  attemptId: string,
  now = Date.now()
): boolean {
  const storage = recoveryStorage();
  if (!storage) return false;
  const record = readRecoveryRecord(storage, now);
  if (!record || record.attemptId !== attemptId) return false;
  if (record.state === 'ready') return true;
  try {
    storage.setItem(
      CHAT_AUTH_RECOVERY_STORAGE_KEY,
      JSON.stringify({ ...record, state: 'ready' })
    );
    return true;
  } catch {
    return false;
  }
}

export function clearArmedChatAuthRecovery(
  attemptId: string,
  now = Date.now()
): void {
  const storage = recoveryStorage();
  if (!storage) return;
  const record = readRecoveryRecord(storage, now);
  if (record?.attemptId === attemptId && record.state === 'armed') {
    removeRecoveryRecord(storage);
  }
}

export function discardChatAuthRecovery(
  attemptId: string,
  now = Date.now()
): void {
  const storage = recoveryStorage();
  if (!storage) return;
  const record = readRecoveryRecord(storage, now);
  if (record?.attemptId === attemptId) removeRecoveryRecord(storage);
}

export function consumeChatAuthRecovery(
  owner: { ownerUserId: string; threadId: string },
  now = Date.now()
): string | null {
  const storage = recoveryStorage();
  if (!storage) return null;
  const record = readRecoveryRecord(storage, now);
  if (!record) return null;
  if (record.ownerUserId !== owner.ownerUserId) {
    removeRecoveryRecord(storage);
    return null;
  }
  if (record.state !== 'ready' || record.threadId !== owner.threadId) {
    return null;
  }
  removeRecoveryRecord(storage);
  return record.prompt;
}
