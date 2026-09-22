'use client';

import { Database, Activity } from 'lucide-react';
import { CollapsibleCard } from './CollapsibleCard';

interface ContextPanelProps {
  ragEnabled?: boolean;
  workspaceName?: string | null;
}

interface ConnectorRowProps {
  icon: React.ReactNode;
  name: string;
  status: 'active' | 'idle' | 'offline';
  detail?: string;
}

function ConnectorRow({ icon, name, status, detail }: ConnectorRowProps) {
  const isActive = status === 'active';
  const isOffline = status === 'offline';

  return (
    <li
      className={
        'group flex items-center gap-3 py-2 -mx-1 px-1 rounded transition-colors hover:bg-(--nous-aurum)/30 dark:hover:bg-(--nous-ember)/30 ' +
        (isOffline ? 'opacity-55' : '')
      }
    >
      <span
        className={
          'flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors ' +
          (isActive
            ? 'bg-(--nous-aurum) dark:bg-(--nous-ember) border border-[rgba(var(--nous-sol-rgb),0.25)]'
            : 'bg-(--nous-bg-1) dark:bg-(--nous-nyx) border border-(--nous-border-1) dark:border-(--nous-shade)')
        }
      >
        <span
          className={
            'h-4 w-4 ' +
            (isActive
              ? 'text-(--nous-sol-safe) dark:text-(--nous-helios)'
              : 'text-(--nous-fg-3)')
          }
        >
          {icon}
        </span>
      </span>

      <div className="flex-1 min-w-0">
        <p
          className="text-[13px] font-medium truncate text-(--nous-fg-1)"
          style={{ fontFamily: 'var(--nous-font-ui)' }}
        >
          {name}
        </p>
        <p
          className="text-[10px] mt-0.5 truncate text-(--nous-fg-3)"
          style={{
            fontFamily: 'var(--nous-font-mono)',
            letterSpacing: '0.04em',
          }}
        >
          {detail ?? (isActive ? 'Active' : isOffline ? 'Offline' : 'Idle')}
        </p>
      </div>

      <span
        className={
          'w-1.5 h-1.5 rounded-full shrink-0 ' +
          (isActive
            ? 'bg-(--nous-sol) dark:bg-(--nous-helios)'
            : isOffline
              ? 'bg-(--nous-fg-3)/40'
              : 'bg-(--nous-fg-3)')
        }
        style={
          isActive
            ? {
                boxShadow: '0 0 0 2px rgba(var(--nous-sol-rgb), 0.18)',
                animation: 'nous-pulse 2s ease-in-out infinite',
              }
            : undefined
        }
        aria-label={status}
      />
    </li>
  );
}

export function ContextPanel({
  ragEnabled = false,
  workspaceName,
}: ContextPanelProps) {
  return (
    <CollapsibleCard
      title="Context"
      icon={<Activity className="h-3 w-3" strokeWidth={1.7} />}
    >
      <div className="mb-2 mt-1 text-xs text-(--nous-fg-3)">Connectors</div>
      <ul className="space-y-0.5">
        <ConnectorRow
          icon={<Database className="h-4 w-4" strokeWidth={1.7} />}
          name="Workspace RAG"
          status={ragEnabled ? 'active' : 'idle'}
          detail={
            ragEnabled
              ? workspaceName
                ? `Active · ${workspaceName}`
                : 'Active'
              : 'Off'
          }
        />
      </ul>
    </CollapsibleCard>
  );
}
