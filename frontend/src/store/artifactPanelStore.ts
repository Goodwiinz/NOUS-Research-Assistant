import { create } from 'zustand';

import type { Citation } from '@/utils/citationParser';

/**
 * What the chat artifact panel is currently showing. `document`/`external`
 * ship first (PR 1); `note`/`draft`/`citations` are wired in follow-ups but
 * the union is fixed here so every producer targets the same shape.
 */
export type Artifact =
  | { kind: 'document'; id: string; title: string }
  | { kind: 'external'; id: string; title: string; source?: string }
  | { kind: 'note'; projectId: string; id: string; title: string }
  | { kind: 'draft'; projectId: string; id: string; title: string }
  | { kind: 'generated'; artifactId: string; versionId: string; title: string }
  // The chat's harness handoff on its own, without another artifact.
  | { kind: 'handoff'; title: string }
  | {
      kind: 'citations';
      citations: Citation[];
      activeCitationId?: string;
      traceId?: string;
    };

export interface OpenArtifactOptions {
  /**
   * Who initiated the open. `user` (default) always wins; `agent` opens
   * (e.g. auto-focusing a freshly created draft) are ignored while the user
   * has pinned the current artifact.
   */
  source?: 'user' | 'agent';
  /** Captured scope rejects late results from a previous chat/account. */
  scope?: string | null;
}

export type GeneratedArtifact = Extract<Artifact, { kind: 'generated' }>;

interface ArtifactPanelState {
  scope: string | null;
  tabs: GeneratedArtifact[];
  activeVersionId: string | null;
  setScope: (scope: string | null) => void;
  selectTab: (versionId: string) => void;
  closeTab: (versionId: string) => void;
  artifact: Artifact | null;
  isOpen: boolean;
  pinned: boolean;
  openArtifact: (artifact: Artifact, opts?: OpenArtifactOptions) => void;
  /** Hide the panel but keep the artifact so it can be reopened. */
  closePanel: () => void;
  reopenPanel: () => void;
  togglePin: () => void;
  /** Full reset — called on sign-out so a shared-browser account switch
   * never surfaces the previous user's artifact. */
  reset: () => void;
}

/**
 * Pure synchronous UI state for the chat split-view artifact panel — which
 * artifact is in focus and whether the panel is docked open. All content
 * fetching lives in React Query keyed by artifact identity; nothing async is
 * ever written here, so there is no store-race surface (see the PR #1223
 * ledger for why that discipline exists).
 */
export const useArtifactPanelStore = create<ArtifactPanelState>((set, get) => ({
  scope: null,
  tabs: [],
  activeVersionId: null,
  artifact: null,
  isOpen: false,
  pinned: false,

  setScope: (scope) => {
    if (scope !== get().scope) {
      set({
        scope,
        tabs: [],
        activeVersionId: null,
        artifact: null,
        isOpen: false,
        pinned: false,
      });
    }
  },

  selectTab: (versionId) => {
    const artifact = get().tabs.find((tab) => tab.versionId === versionId);
    if (artifact) set({ artifact, activeVersionId: versionId, isOpen: true });
  },

  closeTab: (versionId) => {
    const state = get();
    const index = state.tabs.findIndex((tab) => tab.versionId === versionId);
    if (index < 0) return;
    const tabs = state.tabs.filter((tab) => tab.versionId !== versionId);
    if (state.activeVersionId !== versionId) {
      set({ tabs });
      return;
    }
    const artifact = tabs[Math.min(index, tabs.length - 1)] ?? null;
    set({
      tabs,
      artifact,
      activeVersionId: artifact?.versionId ?? null,
      isOpen: Boolean(artifact),
    });
  },

  openArtifact: (artifact, opts) => {
    if (opts && 'scope' in opts && opts.scope !== get().scope) return;
    const { pinned, isOpen } = get();
    if (opts?.source === 'agent' && pinned && isOpen) return;
    if (artifact.kind === 'generated') {
      const tabs = get().tabs;
      set({
        artifact,
        isOpen: true,
        activeVersionId: artifact.versionId,
        tabs: tabs.some((tab) => tab.versionId === artifact.versionId)
          ? tabs.map((tab) =>
              tab.versionId === artifact.versionId ? artifact : tab
            )
          : [...tabs, artifact],
      });
    } else {
      set({ artifact, isOpen: true, activeVersionId: null });
    }
  },

  closePanel: () => set({ isOpen: false }),

  reopenPanel: () => {
    if (get().artifact) set({ isOpen: true });
  },

  togglePin: () => set((s) => ({ pinned: !s.pinned })),

  reset: () =>
    set({
      scope: null,
      tabs: [],
      activeVersionId: null,
      artifact: null,
      isOpen: false,
      pinned: false,
    }),
}));
