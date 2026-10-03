import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// Real KaTeX (no stub): guards that the sanitizer keeps the markup KaTeX needs
// to draw radicals, stretchy arrows and matrices. A config that strips <svg>
// renders \sqrt{x} without its radical sign and passes the hostile-markup test.
vi.mock('katex/dist/katex.min.css', () => ({}));

import { MathDisplay } from '../MathDisplay';

async function renderLatex(content: string): Promise<HTMLElement> {
  const view = render(<MathDisplay content={content} />);
  await waitFor(() => {
    expect(view.container.querySelector('.katex')).not.toBeNull();
  });
  return view.container;
}

describe('MathDisplay with real KaTeX', () => {
  it.each([
    ['\\sqrt{x}', 'svg path'],
    ['\\overrightarrow{AB}', 'svg path'],
    ['\\underbrace{x+y}_{z}', 'svg path'],
    ['\\cancel{x}', 'svg line'],
  ])('keeps SVG glyphs for %s', async (latex, selector) => {
    const container = await renderLatex(latex);
    expect(container.querySelector(selector)).not.toBeNull();
  });

  it('keeps MathML table structure for matrices', async () => {
    const container = await renderLatex(
      '\\begin{pmatrix}a&b\\\\c&d\\end{pmatrix}'
    );
    expect(container.querySelectorAll('mtd').length).toBe(4);
  });
});
