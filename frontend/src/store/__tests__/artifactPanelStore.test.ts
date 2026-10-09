/**
 * Unit tests for the artifact panel store (Zustand).
 *
 * Pure synchronous UI state: open/close/reopen semantics and the pin gate
 * that stops agent-sourced opens from replacing what the user is reading.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { act } from '@testing-library/react';
import { useArtifactPanelStore, type Artifact } from '../artifactPanelStore';

const doc = (id: string): Artifact => ({
  kind: 'document',
  id,
  title: `Doc ${id}`,
});

describe('artifactPanelStore', () => {
  beforeEach(() => {
    act(() => {
      useArtifactPanelStore.setState({
        artifact: null,
        isOpen: false,
        pinned: false,
      });
    });
  });

  it('starts closed with no artifact', () => {
    const s = useArtifactPanelStore.getState();
    expect(s.artifact).toBeNull();
    expect(s.isOpen).toBe(false);
    expect(s.pinned).toBe(false);
  });

  it('openArtifact sets the artifact and opens the panel', () => {
    act(() => {
      useArtifactPanelStore.getState().openArtifact(doc('a'));
    });
    const s = useArtifactPanelStore.getState();
    expect(s.artifact).toEqual(doc('a'));
    expect(s.isOpen).toBe(true);
  });

  it('a user open replaces the current artifact', () => {
    act(() => {
      useArtifactPanelStore.getState().openArtifact(doc('a'));
      useArtifactPanelStore.getState().openArtifact(doc('b'));
    });
    expect(useArtifactPanelStore.getState().artifact).toEqual(doc('b'));
  });

  it('closePanel hides but keeps the artifact; reopenPanel restores it', () => {
    act(() => {
      useArtifactPanelStore.getState().openArtifact(doc('a'));
      useArtifactPanelStore.getState().closePanel();
    });
    let s = useArtifactPanelStore.getState();
    expect(s.isOpen).toBe(false);
    expect(s.artifact).toEqual(doc('a'));

    act(() => {
      useArtifactPanelStore.getState().reopenPanel();
    });
    s = useArtifactPanelStore.getState();
    expect(s.isOpen).toBe(true);
    expect(s.artifact).toEqual(doc('a'));
  });

  it('reopenPanel is a no-op with no artifact', () => {
    act(() => {
      useArtifactPanelStore.getState().reopenPanel();
    });
    expect(useArtifactPanelStore.getState().isOpen).toBe(false);
  });

  it('agent-sourced open is ignored while pinned and open', () => {
    act(() => {
      useArtifactPanelStore.getState().openArtifact(doc('a'));
      useArtifactPanelStore.getState().togglePin();
      useArtifactPanelStore
        .getState()
        .openArtifact(doc('b'), { source: 'agent' });
    });
    expect(useArtifactPanelStore.getState().artifact).toEqual(doc('a'));
  });

  it('agent-sourced open lands when the panel is pinned but closed', () => {
    act(() => {
      useArtifactPanelStore.getState().openArtifact(doc('a'));
      useArtifactPanelStore.getState().togglePin();
      useArtifactPanelStore.getState().closePanel();
      useArtifactPanelStore
        .getState()
        .openArtifact(doc('b'), { source: 'agent' });
    });
    const s = useArtifactPanelStore.getState();
    expect(s.artifact).toEqual(doc('b'));
    expect(s.isOpen).toBe(true);
  });

  it('user open still replaces a pinned artifact', () => {
    act(() => {
      useArtifactPanelStore.getState().openArtifact(doc('a'));
      useArtifactPanelStore.getState().togglePin();
      useArtifactPanelStore
        .getState()
        .openArtifact(doc('b'), { source: 'user' });
    });
    expect(useArtifactPanelStore.getState().artifact).toEqual(doc('b'));
  });
});

const generated = (versionId: string): Artifact => ({
  kind: 'generated',
  artifactId: 'a1',
  versionId,
  title: `${versionId}.txt`,
});

describe('generated artifact tabs', () => {
  beforeEach(() => useArtifactPanelStore.getState().reset());

  it('keeps ordered exact versions and deduplicates repeated opens', () => {
    const store = useArtifactPanelStore.getState();
    store.setScope('scope-a');
    store.openArtifact(generated('v1'), { scope: 'scope-a' });
    store.openArtifact(generated('v2'), { scope: 'scope-a' });
    store.openArtifact(generated('v1'), { scope: 'scope-a' });
    expect(
      useArtifactPanelStore.getState().tabs.map((t) => t.versionId)
    ).toEqual(['v1', 'v2']);
    expect(useArtifactPanelStore.getState().activeVersionId).toBe('v1');
  });

  it('selects the nearest remaining version when closing and hides the final tab', () => {
    const store = useArtifactPanelStore.getState();
    store.openArtifact(generated('v1'));
    store.openArtifact(generated('v2'));
    store.closeTab('v2');
    expect(useArtifactPanelStore.getState().artifact).toEqual(generated('v1'));
    store.closeTab('v1');
    expect(useArtifactPanelStore.getState().isOpen).toBe(false);
  });

  it('clears every tab synchronously on scope change and rejects a stale open', () => {
    const store = useArtifactPanelStore.getState();
    store.setScope('scope-a');
    store.openArtifact(generated('v1'), { scope: 'scope-a' });
    store.togglePin();
    store.setScope('scope-b');
    store.openArtifact(generated('v9'), { scope: 'scope-a' });
    expect(useArtifactPanelStore.getState()).toMatchObject({
      scope: 'scope-b',
      tabs: [],
      artifact: null,
      activeVersionId: null,
      isOpen: false,
      pinned: false,
    });
  });
});
