import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// KaTeX output is injected via dangerouslySetInnerHTML, so the DOMPurify pass
// is the only thing standing between renderer output and the DOM. Stub KaTeX
// to emit hostile markup and prove the sanitizer strips it.
vi.mock('katex', () => ({
  default: {
    renderToString: () =>
      '<span class="katex">x<sup>2</sup></span>' +
      '<img src="x" onerror="window.__pwned = true">' +
      '<a href="javascript:alert(1)">link</a>' +
      '<script>window.__pwned = true</script>' +
      '<span class="katex-html" onclick="window.__pwned = true">y</span>',
  },
}));
vi.mock('katex/dist/katex.min.css', () => ({}));

import { MathDisplay } from '../MathDisplay';

describe('MathDisplay', () => {
  it('sanitizes KaTeX HTML before injecting it', async () => {
    const { container } = render(<MathDisplay content="x^2" />);

    await waitFor(() => {
      expect(container.querySelector('.katex')).not.toBeNull();
    });

    expect(container.querySelector('script')).toBeNull();
    expect(container.innerHTML).not.toContain('onerror');
    expect(container.innerHTML).not.toContain('javascript:');
    expect(container.innerHTML).not.toContain('onclick');
    expect(container.querySelector('.katex-html')?.textContent).toBe('y');
  });

  it('renders markdown format as escaped text without KaTeX', () => {
    const { container } = render(
      <MathDisplay content="<b>raw</b>" format="markdown" />
    );

    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toBe('<b>raw</b>');
  });
});
