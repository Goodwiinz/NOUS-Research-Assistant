import { describe, expect, it } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render } from '@testing-library/react';

import AuthScenePanel from '@/components/auth/AuthScenePanel';

describe('AuthScenePanel', () => {
  it.each(['heavy', 'light'] as const)(
    'washes the top edge behind the wordmark at every breakpoint (%s wash)',
    (wash) => {
      const { container } = render(
        <AuthScenePanel
          src="/x.svg"
          headline="Headline"
          body="Body"
          notes={['Note']}
          wash={wash}
          proseTone="ivory"
        />
      );
      const top = container.querySelector('[data-wash="top"]') as HTMLElement;
      expect(top).not.toBeNull();
      expect(top).toHaveAttribute('aria-hidden', 'true');
      // Not breakpoint-gated: present on mobile and desktop alike.
      expect(top.className).not.toMatch(/(^|\s)(hidden|lg:hidden)(\s|$)/);
      // Holds at least 0.8 Nyx behind the text: ivory and parchment stay AA
      // even over a pure white pixel of the illustration.
      expect(top.style.background).toMatch(/0\.85\)/);
    }
  );
});
