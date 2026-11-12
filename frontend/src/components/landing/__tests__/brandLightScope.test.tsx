import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';

import AuthLayout from '../../../../app/(auth)/layout';
import HomePage from '../../../../app/page';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ isAuthenticated: false }),
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: ({
    children,
    href,
    ...props
  }: {
    children: ReactNode;
    href: string;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

const read = (rel: string): string =>
  readFileSync(resolve(process.cwd(), rel), 'utf8');

const tokens = read('app/nous-tokens.css');
const globals = read('app/globals.css');

/** Body of the first rule whose selector list is exactly `selector`. */
function ruleBody(css: string, selector: RegExp): string {
  const match = selector.exec(css);
  if (!match) return '';
  const start = match.index + match[0].length;
  return css.slice(start, css.indexOf('}', start));
}

describe('always-light brand scope (.nous-light)', () => {
  it('re-declares the light --nous-* tokens inside the scope', () => {
    expect(tokens).toMatch(/:root,\s*\.nous-light\s*\{/);
  });

  it('re-declares the light shadcn and sidebar tokens inside the scope', () => {
    expect(globals.match(/:root,\s*\.nous-light\s*\{/g)).toHaveLength(2);
  });

  it('re-resolves every theme-adaptive Tailwind colour inside the scope', () => {
    // @theme vars are computed once on :root, so a scope that only redefined
    // --background would still inherit the dark --color-background.
    const theme = ruleBody(globals, /@theme\s*\{/);
    const adaptive = theme
      .split('\n')
      .map((line) => line.trim())
      .filter((line) =>
        /^--color-[\w-]+:\s*(hsl\(var\(--|var\(--sidebar-)/.test(line)
      );
    expect(adaptive.length).toBeGreaterThan(20);

    // The dedicated scope rule (the light :root blocks also list .nous-light).
    const scope = ruleBody(globals, /[;}/]\s*\.nous-light\s*\{/);
    expect(scope).toContain('color-scheme: light');
    adaptive.forEach((decl) => expect(scope).toContain(decl));
  });

  it('keeps dark: utilities from matching inside the scope without changing their specificity', () => {
    expect(globals).toMatch(
      /&:is\(\.dark \*\):not\(:where\(\.nous-light, \.nous-light \*\)\)/
    );
    expect(globals).not.toMatch(/@custom-variant dark \(&:is\(\.dark \*\)\);/);
  });

  it('keeps the dark autofill override out of the scope', () => {
    expect(globals).toMatch(
      /\.dark input:-webkit-autofill:not\(:where\(\.nous-light \*\)\)/
    );
  });

  it('is applied once on the landing root', () => {
    const { container } = render(<HomePage />);
    expect(container.firstElementChild).toHaveClass('nous-light');
    expect(container.querySelectorAll('.nous-light')).toHaveLength(1);
  });

  it('is applied once on the auth layout', () => {
    const { container } = render(
      <AuthLayout>
        <p>child</p>
      </AuthLayout>
    );
    expect(container.firstElementChild).toHaveClass('nous-light');
    expect(container.firstElementChild).toHaveClass('text-foreground');
  });
});

/** WCAG 2.x relative luminance contrast. */
function contrast(a: string, b: string): number {
  const lum = (hex: string): number => {
    const [r, g, bl] = [1, 3, 5]
      .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const token = (name: string): string => {
  const m = new RegExp(`--nous-${name}:\\s*(#[0-9a-fA-F]{6})`).exec(tokens);
  if (!m) throw new Error(`--nous-${name} not found`);
  return m[1];
};

describe('--nous-sol-safe', () => {
  it.each(['aurum', 'selene'])(
    'reaches AA (4.5:1) for text on %s',
    (ground) => {
      expect(contrast(token('sol-safe'), token(ground))).toBeGreaterThanOrEqual(
        4.5
      );
    }
  );
});
