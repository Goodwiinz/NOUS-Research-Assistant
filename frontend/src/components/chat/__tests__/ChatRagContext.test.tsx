import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReactElement } from 'react';

import { ChatRagProvider, useChatRag } from '../ChatRagContext';

function ToggleHarness(): ReactElement {
  const { ragEnabled, setRagEnabled } = useChatRag();
  return (
    <button type="button" onClick={() => setRagEnabled((enabled) => !enabled)}>
      {String(ragEnabled)}
    </button>
  );
}

describe('ChatRagProvider', () => {
  it('owns one toggle that can move in both directions', () => {
    render(
      <ChatRagProvider>
        <ToggleHarness />
      </ChatRagProvider>
    );

    const toggle = screen.getByRole('button');
    expect(toggle).toHaveTextContent('true');
    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent('false');
    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent('true');
  });
});
