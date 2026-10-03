import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AuthProvider } from '@/hooks/useAuth';
import { useAuthStore } from '@/stores/authStore';
import { resetAccountSession } from '@/lib/account-session';

const initialize = useAuthStore.getState().initialize;
afterEach(() => {
  useAuthStore.setState({ initialize });
  vi.restoreAllMocks();
});

it('remounts private page state on account reset but preserves it during same-user updates', () => {
  useAuthStore.setState({
    initialize: vi.fn(),
    user: { id: 'A' } as never,
    isAuthenticated: true,
  });
  function PrivatePage(): React.JSX.Element {
    const [draft, setDraft] = useState('');
    return (
      <input
        aria-label="Private draft"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
    );
  }
  render(
    <AuthProvider>
      <PrivatePage />
    </AuthProvider>
  );
  fireEvent.change(screen.getByLabelText('Private draft'), {
    target: { value: 'A private note' },
  });
  act(() => {
    useAuthStore.setState({ user: { id: 'A' } as never });
  });
  expect(screen.getByLabelText('Private draft')).toHaveValue('A private note');
  act(() => {
    resetAccountSession();
    useAuthStore.setState({ user: { id: 'B' } as never });
  });
  expect(screen.getByLabelText('Private draft')).toHaveValue('');
});
