import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import SettingsPage from '../../../../app/(dashboard)/settings/page';

const mockedAuth = {
  user: {
    email: 'allocs16@gmail.com',
  },
};

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockedAuth,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    back: vi.fn(),
    push: vi.fn(),
  }),
}));

describe('Settings page', () => {
  it('renders the settings overview hub', () => {
    render(<SettingsPage />);

    expect(
      screen.getByRole('heading', { name: 'Settings' })
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /manage your account, workspace governance, model access, and platform controls/i
      )
    ).toBeInTheDocument();
    expect(screen.getByText('allocs16@gmail.com')).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('Role')).toBeInTheDocument();
    expect(screen.getByText('Plan')).toBeInTheDocument();
    expect(screen.getByText('API access')).toBeInTheDocument();
    expect(screen.getByText('Profile and preferences')).toBeInTheDocument();
    expect(screen.getByText('Workspace and organization')).toBeInTheDocument();
    expect(screen.getByText('Developer access')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /open workspace settings/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /open developer access/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /manage connected devices/i })
    ).toHaveAttribute('href', '/integrations/devices');
    expect(screen.getByText(/last sign-in/i)).toBeInTheDocument();
    // Honest empty states replace fabricated status values.
    expect(screen.getByText('No workspace connected')).toBeInTheDocument();
    expect(screen.getByText('No tokens yet')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /jump to personal controls/i })
    ).toHaveAttribute('href', '#personal-preferences');
    expect(
      screen.queryByRole('link', { name: /review profile & preferences/i })
    ).not.toBeInTheDocument();
  });

  it('shows inline preference controls and no modal affordances', () => {
    render(<SettingsPage />);

    const emailNotifications = screen.getByRole('switch', {
      name: /email notifications/i,
    });
    expect(emailNotifications).toBeChecked();

    fireEvent.click(emailNotifications);
    expect(emailNotifications).not.toBeChecked();

    expect(
      screen.queryByRole('button', { name: 'Close settings' })
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId('settings-panel')).not.toBeInTheDocument();
  });
});
