import { useCallback, useSyncExternalStore } from 'react';
import { getChatSessionSignal, onChatSessionReset } from '@/store/chat-store';

/** Bind rendered actions to the account that produced their private inputs. */
export function useChatSessionGuard(): () => boolean {
  const signal = useSyncExternalStore(
    onChatSessionReset,
    getChatSessionSignal,
    getChatSessionSignal
  );
  return useCallback(() => !signal.aborted, [signal]);
}
