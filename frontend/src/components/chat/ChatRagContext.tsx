'use client';

import {
  createContext,
  useContext,
  useMemo,
  useState,
  type Dispatch,
  type ReactElement,
  type ReactNode,
  type SetStateAction,
} from 'react';

interface ChatRagContextValue {
  ragEnabled: boolean;
  setRagEnabled: Dispatch<SetStateAction<boolean>>;
}

const defaultValue: ChatRagContextValue = {
  ragEnabled: true,
  setRagEnabled: () => undefined,
};

const ChatRagContext = createContext<ChatRagContextValue>(defaultValue);

export function ChatRagProvider({
  children,
}: {
  children: ReactNode;
}): ReactElement {
  const [ragEnabled, setRagEnabled] = useState(true);
  const value = useMemo(() => ({ ragEnabled, setRagEnabled }), [ragEnabled]);

  return (
    <ChatRagContext.Provider value={value}>{children}</ChatRagContext.Provider>
  );
}

export function useChatRag(): ChatRagContextValue {
  return useContext(ChatRagContext);
}
