import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FooterSection } from '@/components/landing/FooterSection';

const hrefs = (container: HTMLElement): (string | null)[] =>
  Array.from(container.querySelectorAll('footer a')).map((a) =>
    a.getAttribute('href')
  );

describe('FooterSection', () => {
  it('hides the gated Search and Dashboard links from signed-out visitors', () => {
    const { container } = render(<FooterSection isAuthenticated={false} />);
    expect(hrefs(container)).not.toContain('/search');
    expect(hrefs(container)).not.toContain('/dashboard');
    expect(hrefs(container)).toContain('/#what');
    expect(screen.getByRole('contentinfo')).toBeInTheDocument();
  });

  it('shows them to signed-in visitors', () => {
    const { container } = render(<FooterSection isAuthenticated />);
    expect(hrefs(container)).toContain('/search');
    expect(hrefs(container)).toContain('/dashboard');
  });
});
