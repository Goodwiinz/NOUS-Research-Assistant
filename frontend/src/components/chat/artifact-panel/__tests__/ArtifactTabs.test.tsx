import { act, screen } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import { render } from '@/test/test-utils';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { ArtifactTabs } from '../ArtifactTabs';

beforeEach(() => {
  useArtifactPanelStore.getState().reset();
  for (const versionId of ['v1', 'v2', 'v3']) {
    useArtifactPanelStore
      .getState()
      .openArtifact({
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
