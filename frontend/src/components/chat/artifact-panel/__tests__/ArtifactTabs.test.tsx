import { act, screen } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import { render } from '@/test/test-utils';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { ArtifactTabs } from '../ArtifactTabs';

beforeEach(() => {
  useArtifactPanelStore.getState().reset();
  for (const versionId of ['v1', 'v2', 'v3']) {
    useArtifactPanelStore.getState().openArtifact({
      kind: 'generated',
      artifactId: 'a',
      versionId,
      title: `File ${versionId}`,
    });
  }
});

it('provides keyboard selection and moves focus to the nearest tab after close', async () => {
  const { user } = render(<ArtifactTabs />);
  const tabs = screen.getAllByRole('tab');
  expect(tabs).toHaveLength(3);
  expect(tabs[2]).toHaveAttribute('aria-selected', 'true');
  act(() => tabs[2].focus());
  await user.keyboard('{Home}');
  expect(tabs[0]).toHaveFocus();
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('v1');
  await user.keyboard('{ArrowRight}{Delete}');
  expect(screen.getAllByRole('tab')).toHaveLength(2);
  expect(screen.getByRole('tab', { name: 'File v3' })).toHaveFocus();
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('v3');
});

it('closing a background tab preserves the active version and focus', async () => {
  const { user } = render(<ArtifactTabs />);
  await user.click(
    screen.getByRole('button', { name: 'Close File v1 version v1' })
  );
  expect(useArtifactPanelStore.getState().activeVersionId).toBe('v3');
  expect(screen.getByRole('tab', { name: 'File v3' })).toHaveFocus();
});

it('returns focus to the file card when the final version is closed', async () => {
  act(() => {
    useArtifactPanelStore.getState().closeTab('v1');
    useArtifactPanelStore.getState().closeTab('v2');
  });
  const { user } = render(
    <>
      <button data-artifact-version="v3">Open file</button>
      <ArtifactTabs />
    </>
  );
  await user.click(
    screen.getByRole('button', { name: 'Close File v3 version v3' })
  );
  expect(screen.getByRole('button', { name: 'Open file' })).toHaveFocus();
  expect(useArtifactPanelStore.getState().isOpen).toBe(false);
});
