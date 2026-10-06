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
}

interface ArtifactPanelState {
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
  artifact: null,
  isOpen: false,
  pinned: false,

  openArtifact: (artifact, opts) => {
    const { pinned, isOpen } = get();
    if (opts?.source === 'agent' && pinned && isOpen) return;
    set({ artifact, isOpen: true });
  },

  closePanel: () => set({ isOpen: false }),

  reopenPanel: () => {
    if (get().artifact) set({ isOpen: true });
  },

  togglePin: () => set((s) => ({ pinned: !s.pinned })),

  reset: () => set({ artifact: null, isOpen: false, pinned: false }),
}));
