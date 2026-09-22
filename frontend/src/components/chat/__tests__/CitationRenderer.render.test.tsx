/**
 * Direct rendering tests for CitationRenderer / ChatMarkdown.
 *
 * Most chat-page tests mock CitationRenderer away, so this file is the
 * regression net for the actual markdown output: GFM (tables, task lists,
 * strikethrough), fenced/inline code, safe links, and markdown interleaved
 * with [Doc N] citation markers — in BOTH the plain and citation-segmented
 * paths, which previously had divergent configs.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { CitationRenderer } from '../CitationRenderer';
import { completeStreamingMarkdown } from '@/lib/markdown-utils';
import type { Citation } from '@/utils/citationParser';

// The syntax highlighter is loaded via next/dynamic (ssr: false); replace it
// with a synchronous stub so fenced-code assertions don't race React.lazy.
vi.mock('next/dynamic', () => ({
  default: () => {
    const MockHighlighter = ({
      language,
      children,
    }: {
      language?: string;
      children?: React.ReactNode;
    }): React.ReactElement => (
      <pre data-testid="syntax-highlighter" data-language={language}>
        <code>{children}</code>
      </pre>
    );
    return MockHighlighter;
  },
}));

const CITATIONS: Citation[] = [
  { documentId: 'doc-1', title: 'Attention Is All You Need', score: 0.9 },
];

describe('CitationRenderer — plain path (no citation markers)', () => {
  it('renders headings', () => {
    render(<CitationRenderer content={'## Results\n\nBody text.'} />);
    expect(
      screen.getByRole('heading', { level: 2, name: 'Results' })
    ).toBeInTheDocument();
  });

  it('renders GFM tables', () => {
    const table = '| Model | Score |\n| --- | --- |\n| BERT | 82.1 |';
    render(<CitationRenderer content={table} />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(
      screen.getByRole('columnheader', { name: 'Model' })
    ).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '82.1' })).toBeInTheDocument();
  });

  it('renders GFM strikethrough', () => {
    const { container } = render(
      <CitationRenderer content={'~~outdated~~ current'} />
    );
    expect(container.querySelector('del')).toHaveTextContent('outdated');
  });

  it('renders GFM task lists', () => {
    render(<CitationRenderer content={'- [x] done\n- [ ] todo'} />);
    const boxes = screen.getAllByRole('checkbox');
    expect(boxes).toHaveLength(2);
    expect(boxes[0]).toBeChecked();
    expect(boxes[1]).not.toBeChecked();
  });

  it('routes fenced code with a language through the syntax highlighter', () => {
    render(<CitationRenderer content={'```python\nprint("hi")\n```'} />);
    const block = screen.getByTestId('syntax-highlighter');
    expect(block).toHaveAttribute('data-language', 'python');
    expect(block).toHaveTextContent('print("hi")');
  });

  it('renders fenced code without a language as a plain code block', () => {
    const { container } = render(
      <CitationRenderer content={'```\nplain block\nline two\n```'} />
    );
    expect(screen.queryByTestId('syntax-highlighter')).toBeNull();
    const pre = container.querySelector('pre');
    expect(pre).toHaveTextContent('plain block');
  });

  it('renders a SINGLE-line unlanguaged fence as a block, not an inline pill', () => {
    const { container } = render(
      <CitationRenderer content={'```\npnpm install\n```'} />
    );
    const pre = container.querySelector('pre');
    expect(pre).toHaveTextContent('pnpm install');
  });

  it('renders GFM autolinks (bare URLs) with the security attributes', () => {
    render(
      <CitationRenderer content={'See https://arxiv.org/abs/1706.03762 now.'} />
    );
    const link = screen.getByRole('link', {
      name: 'https://arxiv.org/abs/1706.03762',
    });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('renders inline code as a styled span, not a block', () => {
    const { container } = render(
      <CitationRenderer content={'Use `pnpm dev` locally.'} />
    );
    const code = container.querySelector('code');
    expect(code).toHaveTextContent('pnpm dev');
    expect(container.querySelector('pre')).toBeNull();
  });

  it('opens links in a new tab with noopener noreferrer', () => {
    render(
      <CitationRenderer content={'[arXiv](https://arxiv.org/abs/1706.03762)'} />
    );
    const link = screen.getByRole('link', { name: 'arXiv' });
    expect(link).toHaveAttribute('href', 'https://arxiv.org/abs/1706.03762');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });
});

describe('CitationRenderer — citation-segmented path', () => {
  it('renders a citation link between markdown fragments', () => {
    const { container } = render(
      <CitationRenderer
        content={'Transformers use **attention** [Doc 1] heavily.'}
        citations={CITATIONS}
      />
    );
    expect(container.querySelector('strong')).toHaveTextContent('attention');
    expect(screen.getAllByText('1').length).toBeGreaterThan(0);
    const paragraph = container.querySelector('p');
    expect(paragraph).toBeTruthy();
    expect(paragraph).toContainElement(
      screen.getByRole('button', { name: /^Source 1/ })
    );
  });

  it('preserves paragraph rhythm around inline citations', () => {
    const { container } = render(
      <CitationRenderer
        content={
          'First paragraph.\n\nSecond paragraph [Doc 1].\n\nThird paragraph.'
        }
        citations={CITATIONS}
      />
    );

    expect(container.querySelectorAll('p')).toHaveLength(3);
  });

  it('keeps a citation inside its list item without splitting the list', () => {
    const { container } = render(
      <CitationRenderer
        content={'- First finding [Doc 1]\n- Second finding\n- Third finding'}
        citations={CITATIONS}
      />
    );

    expect(container.querySelectorAll('ul')).toHaveLength(1);
    expect(container.querySelectorAll('li')).toHaveLength(3);
    expect(container.querySelector('li')).toContainElement(
      screen.getByRole('button', { name: /^Source 1/ })
    );
  });

  it('keeps citations inside headings, emphasis, quotes, and table cells', () => {
    const content = [
      '## Evidence [Doc 1]',
      '',
      '**Strong evidence [Doc 1]** supports the claim.',
      '',
      '> The study reports an improvement [Doc 1].',
      '',
      '| Finding | Evidence |',
      '| --- | --- |',
      '| Accuracy | Improved [Doc 1] |',
    ].join('\n');
    const { container } = render(
      <CitationRenderer content={content} citations={CITATIONS} />
    );

    expect(screen.getByRole('heading', { level: 2 })).toContainElement(
      screen.getAllByRole('button', { name: /^Source 1/ })[0]
    );
    expect(container.querySelector('strong')).toContainElement(
      screen.getAllByRole('button', { name: /^Source 1/ })[1]
    );
    expect(container.querySelector('blockquote')).toContainElement(
      screen.getAllByRole('button', { name: /^Source 1/ })[2]
    );
    expect(screen.getByRole('cell', { name: /Improved/ })).toContainElement(
      screen.getAllByRole('button', { name: /^Source 1/ })[3]
    );
  });

  it('preserves linked emphasis beside a citation', () => {
    const { container } = render(
      <CitationRenderer
        content={'See **[the paper](https://example.com) [Doc 1]** now.'}
        citations={CITATIONS}
      />
    );

    const strong = container.querySelector('strong');
    expect(strong).toContainElement(
      screen.getByRole('link', { name: 'the paper' })
    );
    expect(strong).toContainElement(
      screen.getByRole('button', { name: /^Source 1/ })
    );
    expect(container).not.toHaveTextContent('**');
  });

  it('uses the polished reading measure without constraining table overflow', () => {
    const { container } = render(
      <CitationRenderer
        content={'| Model | Score |\n| --- | --- |\n| BERT | 82.1 |'}
      />
    );

    expect(container.firstElementChild).toHaveClass(
      'nous-prose',
      'w-full',
      'max-w-[72ch]'
    );
    expect(screen.getByRole('table').parentElement).toHaveClass(
      'overflow-x-auto'
    );
  });

  it('keeps the same document structure when streaming citations resolve', () => {
    const content = '- Supported finding [Doc 1]\n- Follow-up finding';
    const view = render(<CitationRenderer content={content} citations={[]} />);

    expect(view.container.querySelectorAll('ul')).toHaveLength(1);
    expect(view.container.querySelectorAll('li')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Source 1' })).toBeDisabled();

    view.rerender(<CitationRenderer content={content} citations={CITATIONS} />);
    expect(view.container.querySelectorAll('ul')).toHaveLength(1);
    expect(view.container.querySelectorAll('li')).toHaveLength(2);
    expect(screen.getByRole('button', { name: /^Source 1:/ })).toBeEnabled();
  });

  it('keeps bracketed indexing inside a code fence as code, not a chip', () => {
    const content = 'Per the paper [Doc 1]:\n\n```python\na = arr[1]\n```';
    render(<CitationRenderer content={content} citations={CITATIONS} />);
    const block = screen.getByTestId('syntax-highlighter');
    expect(block).toHaveTextContent('a = arr[1]');
    // Exactly one chip (the prose [Doc 1]); arr[1] must not become one.
    expect(screen.getAllByRole('button', { name: /^Source 1/ })).toHaveLength(
      1
    );
  });

  it('leaves explicit citation syntax inside inline code untouched', () => {
    render(
      <CitationRenderer
        content={'Write `[Doc 1]` literally, then cite it [Doc 1].'}
        citations={CITATIONS}
      />
    );

    expect(screen.getByText('[Doc 1]').tagName).toBe('CODE');
    expect(screen.getAllByRole('button', { name: /^Source 1/ })).toHaveLength(
      1
    );
  });

  it('applies the same citation transform on the lazy math path', async () => {
    const { container } = render(
      <CitationRenderer
        content={'The relation is $E=mc^2$ [Doc 1].'}
        citations={CITATIONS}
      />
    );

    expect(
      await screen.findByRole('button', { name: /^Source 1/ })
    ).toBeInTheDocument();
    await waitFor(() => expect(container.querySelector('.katex')).toBeTruthy());
  });

  it('renders an unresolvable citation index as a disabled chip', () => {
    const onClick = vi.fn();
    render(
      <CitationRenderer
        content={'See [Doc 3].'}
        citations={CITATIONS}
        onCitationClick={onClick}
      />
    );
    const chip = screen.getByRole('button', { name: 'Source 3' });
    expect(chip).toBeDisabled();
    chip.click();
    expect(onClick).not.toHaveBeenCalled();
  });

  it('renders GFM tables in a message that also contains citations', () => {
    const content =
      'Summary [Doc 1]\n\n| Model | Score |\n| --- | --- |\n| BERT | 82.1 |';
    render(<CitationRenderer content={content} citations={CITATIONS} />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '82.1' })).toBeInTheDocument();
  });

  it('renders fenced code with citations elsewhere in the message', () => {
    const content = 'Per the paper [Doc 1]:\n\n```python\nx = 1\n```';
    render(<CitationRenderer content={content} citations={CITATIONS} />);
    const block = screen.getByTestId('syntax-highlighter');
    expect(block).toHaveAttribute('data-language', 'python');
  });

  it('fires onCitationClick with the resolved citation', () => {
    const onClick = vi.fn();
    render(
      <CitationRenderer
        content={'See [Doc 1].'}
        citations={CITATIONS}
        onCitationClick={onClick}
      />
    );
    screen.getByText('1').click();
    expect(onClick).toHaveBeenCalledWith(
      expect.objectContaining({ documentId: 'doc-1' })
    );
  });
});

describe('CitationRenderer — streaming (unterminated markdown)', () => {
  it('renders a dangling code fence as a highlighted block mid-stream', () => {
    const partial = 'Working:\n\n```python\nprint("hi';
    render(<CitationRenderer content={completeStreamingMarkdown(partial)} />);
    const block = screen.getByTestId('syntax-highlighter');
    expect(block).toHaveAttribute('data-language', 'python');
    expect(block).toHaveTextContent('print("hi');
  });

  it('renders a dangling inline code span without swallowing the line', () => {
    const partial = 'Run `pnpm de';
    const { container } = render(
      <CitationRenderer content={completeStreamingMarkdown(partial)} />
    );
    expect(container.querySelector('code')).toHaveTextContent('pnpm de');
  });
});
