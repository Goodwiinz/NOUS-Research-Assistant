import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import SearchPage from '../../../../../app/(dashboard)/search/page';

vi.mock('@/lib/analytics', () => ({
  getAnalytics: () => ({
    trackPageView: vi.fn(),
    trackSearch: vi.fn(),
  }),
}));

vi.mock('@/components/chat/shared/ChatBubble', () => ({
  ChatBubble: () => <div>Bubble</div>,
}));

vi.mock('@/components/chat/CitationPanel', () => ({
  CitationPanel: () => <div>Citation Panel</div>,
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

describe('SearchPage chat mode', () => {
  it('renders terminal chat composer placeholder', async () => {
    render(<SearchPage />);

    expect(
      await screen.findByPlaceholderText(/message nous/i)
    ).toBeInTheDocument();
  });
});
