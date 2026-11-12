import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';

import { HarnessSelector } from '../HarnessSelector';

type Controller = React.ComponentProps<typeof HarnessSelector>['controller'];

function controller(overrides: Partial<Controller> = {}): Controller {
  return {
    executionProvider: 'nous',
    deviceId: null,
    workspaceId: null,
    devices: [],
    workspaces: [],
    pendingRequests: [],
    connectionState: 'idle',
    statusLabel: 'NOUS',
    disabledReason: null,
    canSend: true,
    runId: null,
    selectProvider: vi.fn(),
    selectDevice: vi.fn(),
    selectWorkspace: vi.fn(),
    isBoundToAnotherChat: () => false,
    refreshDevices: vi.fn(),
    receive: vi.fn(),
    markConnectionLost: vi.fn(),
    loadApproval: vi.fn(),
    decideRequest: vi.fn(),
    ...overrides,
  } as unknown as Controller;
}

describe('HarnessSelector status label', () => {
  it('does not repeat the selected provider as visible status text', () => {
    render(<HarnessSelector controller={controller()} />);

    const status = screen.getByRole('status');
    // Screen readers still get the live status; sighted users see "NOUS" once,
    // in the select.
    expect(status).toHaveTextContent('NOUS');
    expect(status.className).toContain('sr-only');
    expect(status.className).not.toContain('md:inline');
  });

  it('keeps a status that adds information visible', () => {
    render(
      <HarnessSelector
        controller={controller({
          executionProvider: 'codex',
          connectionState: 'connected',
          statusLabel: 'Connected',
        } as Partial<Controller>)}
      />
    );

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('Connected');
    expect(status.className).toContain('md:inline');
    expect(status.className).not.toContain('sr-only');
  });
});
