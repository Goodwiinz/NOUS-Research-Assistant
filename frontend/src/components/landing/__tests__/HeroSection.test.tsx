import { describe, expect, it } from 'vitest';
import { render, screen, type RenderResult } from '@testing-library/react';
import { HeroSection, LandingNav } from '@/components/landing/HeroSection';

// The nav and the hero section are separate exports so app/page.tsx can put
// the hero inside <main> with the nav outside it.
function renderTop(isAuthenticated: boolean): RenderResult {
  return render(
    <>
      <LandingNav isAuthenticated={isAuthenticated} />
      <HeroSection isAuthenticated={isAuthenticated} />
    </>
  );
}

describe('HeroSection', () => {
  it('sends signed-out visitors to register and login', () => {
    renderTop(false);

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
    renderTop(true);

    const dashboard = screen.getAllByRole('link', { name: /open dashboard/i });
    expect(dashboard.length).toBeGreaterThan(0);
    dashboard.forEach((link) =>
      expect(link).toHaveAttribute('href', '/dashboard')
    );
    expect(
      screen.queryByRole('link', { name: /sign in/i })
    ).not.toBeInTheDocument();
  });

  it('gives the wordmark link the house focus ring and a hover state', () => {
    render(<LandingNav isAuthenticated={false} />);
    const wordmark = screen.getByRole('link', { name: /^NOUS/ });
    expect(wordmark).toHaveAttribute('href', '/');
    expect(wordmark.className).toContain('focus-visible:ring-2');
    expect(wordmark.className).toContain('focus-visible:ring-(--nous-sol)/50');
    expect(wordmark.className).toMatch(/hover:/);
  });
});
