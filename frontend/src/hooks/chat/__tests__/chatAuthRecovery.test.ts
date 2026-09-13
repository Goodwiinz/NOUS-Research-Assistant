import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CHAT_AUTH_RECOVERY_STORAGE_KEY,
  clearArmedChatAuthRecovery,
  consumeChatAuthRecovery,
  discardChatAuthRecovery,
  markChatAuthRecoveryReady,
  stageChatAuthRecovery,
} from '@/hooks/chat/chatAuthRecovery';

const ATTEMPT_A = {
  attemptId: 'attempt-A',
  ownerUserId: 'user-A',
  threadId: 'thread-A',
  prompt: '  explain the cited evidence  ',
};

describe('chatAuthRecovery session handoff', () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it('keeps an armed prompt private until the matching auth failure marks it ready', () => {
    expect(stageChatAuthRecovery(ATTEMPT_A, 1_000)).toBe(true);
    expect(
      JSON.parse(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY) ?? '{}')
    ).toEqual({
      version: 1,
      attemptId: 'attempt-A',
      ownerUserId: 'user-A',
      threadId: 'thread-A',
      prompt: 'explain the cited evidence',
      expiresAt: 901_000,
      state: 'armed',
    });
    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-A' },
        2_000
      )
    ).toBeNull();
    expect(markChatAuthRecoveryReady('attempt-B', 2_000)).toBe(false);
    expect(markChatAuthRecoveryReady('attempt-A', 2_000)).toBe(true);

    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-A' },
        2_000
      )
    ).toBe('explain the cited evidence');
    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-A' },
        2_000
      )
    ).toBeNull();
  });

  it('waits for the owning thread without exposing or deleting the prompt', () => {
    stageChatAuthRecovery(ATTEMPT_A, 1_000);
    markChatAuthRecoveryReady('attempt-A', 2_000);

    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-B' },
        2_000
      )
    ).toBeNull();
    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-A' },
        2_000
      )
    ).toBe('explain the cited evidence');
  });

  it("deletes another account's prompt instead of exposing it", () => {
    stageChatAuthRecovery(ATTEMPT_A, 1_000);
    markChatAuthRecoveryReady('attempt-A', 2_000);

    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-B', threadId: 'thread-A' },
        2_000
      )
    ).toBeNull();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('deletes expired and malformed records', () => {
    stageChatAuthRecovery(ATTEMPT_A, 1_000);
    markChatAuthRecoveryReady('attempt-A', 2_000);

    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-A' },
        901_000
      )
    ).toBeNull();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();

    sessionStorage.setItem(CHAT_AUTH_RECOVERY_STORAGE_KEY, '{broken-json');
    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-A' },
        2_000
      )
    ).toBeNull();
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('lets only the owning attempt clear an armed record and preserves ready recovery', () => {
    stageChatAuthRecovery(ATTEMPT_A, 1_000);
    clearArmedChatAuthRecovery('attempt-B', 2_000);
    expect(
      sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)
    ).not.toBeNull();

    markChatAuthRecoveryReady('attempt-A', 2_000);
    clearArmedChatAuthRecovery('attempt-A', 2_000);
    expect(
      sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)
    ).not.toBeNull();

    discardChatAuthRecovery('attempt-B', 2_000);
    expect(
      sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)
    ).not.toBeNull();
    discardChatAuthRecovery('attempt-A', 2_000);
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });

  it('overwrites an older attempt and ignores its late cleanup', () => {
    stageChatAuthRecovery(ATTEMPT_A, 1_000);
    stageChatAuthRecovery(
      {
        attemptId: 'attempt-B',
        ownerUserId: 'user-A',
        threadId: 'thread-B',
        prompt: 'newer prompt',
      },
      2_000
    );

    clearArmedChatAuthRecovery('attempt-A', 3_000);
    expect(markChatAuthRecoveryReady('attempt-B', 3_000)).toBe(true);
    expect(
      consumeChatAuthRecovery(
        { ownerUserId: 'user-A', threadId: 'thread-B' },
        3_000
      )
    ).toBe('newer prompt');
  });

  it('fails closed without blocking recovery when sessionStorage rejects the write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => {
      throw new DOMException('quota exceeded', 'QuotaExceededError');
    });

    expect(stageChatAuthRecovery(ATTEMPT_A, 1_000)).toBe(false);
    expect(sessionStorage.getItem(CHAT_AUTH_RECOVERY_STORAGE_KEY)).toBeNull();
  });
});
