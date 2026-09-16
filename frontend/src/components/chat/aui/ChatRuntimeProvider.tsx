'use client';

import type { ReactElement, ReactNode } from 'react';
import {
  AssistantRuntimeProvider,
  createMessageQueue,
  useExternalStoreRuntime,
} from '@assistant-ui/react';
import {
  useChatRuntimeAdapter,
  type ChatRuntimeProps,
} from '@nous/chat-runtime';
import { NousToolUIs } from './toolUIs';

export interface ChatRuntimeProviderProps extends ChatRuntimeProps {
  children: ReactNode;
}

export function ChatRuntimeProvider({
  children,
  ...props
}: ChatRuntimeProviderProps): ReactElement {
  const runtime = useExternalStoreRuntime(
    useChatRuntimeAdapter(props, createMessageQueue)
  );
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <NousToolUIs />
      {children}
    </AssistantRuntimeProvider>
  );
}
