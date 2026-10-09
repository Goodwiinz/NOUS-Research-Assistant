'use client';

import { useRef, type ReactElement, type KeyboardEvent } from 'react';
import { X } from 'lucide-react';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';

/** Exact committed versions stay addressable; selecting a tab performs no write. */
export function ArtifactTabs(): ReactElement | null {
  const tabs = useArtifactPanelStore((s) => s.tabs);
  const active = useArtifactPanelStore((s) => s.activeVersionId);
  const select = useArtifactPanelStore((s) => s.selectTab);
  const close = useArtifactPanelStore((s) => s.closeTab);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const selectAndFocus = (versionId: string): void => {
    select(versionId);
    if (useArtifactPanelStore.getState().activeVersionId === versionId)
      buttons.current.get(versionId)?.focus();
  };
  const closeAndFocus = (versionId: string): void => {
    const index = tabs.findIndex((tab) => tab.versionId === versionId);
    close(versionId);
    const remaining = useArtifactPanelStore.getState().tabs;
    const selected = useArtifactPanelStore.getState().activeVersionId;
    const next =
      active === versionId
        ? remaining[Math.min(index, remaining.length - 1)]
        : remaining.find((tab) => tab.versionId === selected);
    if (next) selectAndFocus(next.versionId);
    else {
      const opener = Array.from(
        document.querySelectorAll<HTMLElement>('[data-artifact-version]')
      ).find((node) => node.dataset.artifactVersion === versionId);
      (
        opener ??
        document.querySelector<HTMLTextAreaElement>(
          'textarea[aria-label="Message"]'
        )
      )?.focus();
    }
  };
  const onKeyDown = (event: KeyboardEvent, index: number): void => {
    let target: number;
    switch (event.key) {
      case 'Home':
        target = 0;
        break;
      case 'End':
        target = tabs.length - 1;
        break;
      case 'ArrowRight':
        target = (index + 1) % tabs.length;
        break;
      case 'ArrowLeft':
        target = (index + tabs.length - 1) % tabs.length;
        break;
      case 'Delete':
        event.preventDefault();
        closeAndFocus(tabs[index].versionId);
        return;
      default:
        return;
    }
    event.preventDefault();
    selectAndFocus(tabs[target].versionId);
  };
  if (!tabs.length) return null;
  return (
    <div
      className="flex shrink-0 overflow-x-auto border-b border-(--nous-border-1)"
      role="tablist"
      aria-label="Open artifact versions"
    >
      {tabs.map((tab, index) => (
        <div
          key={tab.versionId}
          className="flex shrink-0 items-center border-r border-(--nous-border-1)"
        >
          <button
            ref={(node) => {
              if (node) buttons.current.set(tab.versionId, node);
              else buttons.current.delete(tab.versionId);
            }}
            type="button"
            role="tab"
            aria-selected={active === tab.versionId}
            aria-controls="generated-artifact-panel"
            id={`artifact-tab-${tab.versionId}`}
            tabIndex={
              active === tab.versionId || (!active && index === 0) ? 0 : -1
            }
            className="max-w-44 truncate px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            title={`${tab.title} · ${tab.versionId}`}
            onClick={() => select(tab.versionId)}
            onKeyDown={(event) => onKeyDown(event, index)}
          >
            {tab.title}
          </button>
          <button
            type="button"
            aria-label={`Close ${tab.title} version ${tab.versionId}`}
            onClick={() => closeAndFocus(tab.versionId)}
            className="p-2 focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X aria-hidden="true" className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}
