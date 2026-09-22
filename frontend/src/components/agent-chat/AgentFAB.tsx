'use client';

import React from 'react';
import { Bot, X } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { useAgentChatStore } from '@/store/agentChatStore';

export function AgentFAB() {
  const pathname = usePathname();
  const uiMode = useAgentChatStore((s) => s.uiMode);
  const toggle = useAgentChatStore((s) => s.toggle);
  const hasUnread = useAgentChatStore((s) => s.hasUnread);

  const isOpen = uiMode !== 'closed';
  const isChatRoute = pathname === '/chat' || pathname === '/chat/';

  // /chat already owns its composer. Keep the global panel's close control if
  // it was opened elsewhere, but do not cover Chat's Send button with another
  // launcher once the panel closes.
  if (isChatRoute && !isOpen) return null;

  return (
    <button
      onClick={toggle}
      aria-label={isOpen ? 'Close agent chat' : 'Open agent chat'}
      className="fixed bottom-6 right-6 z-50 h-12 w-12 rounded-full bg-primary text-primary-foreground shadow-lg hover:bg-primary/90 transition-all flex items-center justify-center group"
    >
      {isOpen ? <X className="h-5 w-5" /> : <Bot className="h-5 w-5" />}
      {hasUnread && !isOpen && (
        <span className="absolute -top-1 -right-1 h-3 w-3 rounded-full bg-destructive border-2 border-background" />
      )}
      {/* Keyboard shortcut hint */}
      {!isOpen && (
        <span className="absolute -top-8 right-0 text-[10px] text-muted-foreground bg-popover border border-border rounded px-1.5 py-0.5 opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap">
          ⌘K
        </span>
      )}
    </button>
  );
}
