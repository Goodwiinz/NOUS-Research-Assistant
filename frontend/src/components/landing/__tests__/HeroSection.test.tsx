import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HeroSection } from '@/components/landing/HeroSection';

describe('HeroSection', () => {
  it('sends signed-out visitors to register and login', () => {
    render(<HeroSection isAuthenticated={false} />);

    const getStarted = screen.getAllByRole('link', { name: /get started/i });
    expect(getStarted.length).toBeGreaterThan(0);
    getStarted.forEach((link) =>
      expect(link).toHaveAttribute('href', '/register')
    );
    expect(screen.getByRole('link', { name: /sign in/i })).toHaveAttribute(
      'href',
      '/login'
    );
    expect(
      screen.queryByRole('link', { name: /open dashboard/i })
    ).not.toBeInTheDocument();
  });

  it('sends signed-in visitors to the dashboard', () => {
    render(<HeroSection isAuthenticated />);

    const dashboard = screen.getAllByRole('link', { name: /open dashboard/i });
    expect(dashboard.length).toBeGreaterThan(0);
    dashboard.forEach((link) =>
      expect(link).toHaveAttribute('href', '/dashboard')
    );
    expect(
      screen.queryByRole('link', { name: /sign in/i })
    ).not.toBeInTheDocument();
  });
});
