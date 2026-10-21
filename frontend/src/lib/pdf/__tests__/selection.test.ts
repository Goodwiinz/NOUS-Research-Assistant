import { afterEach, describe, expect, it } from 'vitest';
import { selectedPdfText } from '../selection';
import { composePdfQuestion, quotePdfSelection } from '@/types/pdf-chat';

afterEach(() => {
  document.body.replaceChildren();
});

describe('selected PDF quotation', () => {
  it('quotes only PDF layers, excluding inter-page controls, in physical order', () => {
    const root = document.createElement('div');
    root.innerHTML =
      '<div data-pdf-text-page="0"><span>first</span><br><span>line</span></div><p>private UI text</p><div data-pdf-text-page="1"><span>second</span></div>';
    document.body.append(root);
    const first = root.querySelector('span')!.firstChild!;
    const last = root.querySelector(
      '[data-pdf-text-page="1"] span'
    )!.firstChild!;
    const selection = window.getSelection()!;
    selection.setBaseAndExtent(last, 3, first, 2);
    expect(selectedPdfText(root, selection)).toEqual({
      range: [0, 1],
      quote: '> rst\n> line\n> sec',
    });
    const foreign = document.createTextNode('foreign');
    document.body.append(foreign);
    selection.setBaseAndExtent(foreign, 0, last, 3);
    expect(selectedPdfText(root, selection)).toBeUndefined();
  });

  it('matches multiline prefixing, expansion-before-truncation, and exact UTF-16 bounds', () => {
    expect(quotePdfSelection('one\ntwo')).toBe('> one\n> two');
    const text = `${'a'.repeat(1100)}\n${'z'.repeat(1100)}`;
    expect(quotePdfSelection(text)).toBe(
      `> ${'a'.repeat(997)}...${'z'.repeat(997)}`
    );
    expect(quotePdfSelection('a'.repeat(2000))).toHaveLength(2002);
    expect(quotePdfSelection('a'.repeat(2001))).toHaveLength(1999);
    const expanded = 'a\n'.repeat(600).replace(/\n/g, '\n> ');
    expect(quotePdfSelection('a\n'.repeat(600))).toBe(
      `> ${expanded.slice(0, 997)}...${expanded.slice(-997)}`
    );
  });

  it('normalizes the composed quote and question without trimming, escaping, or capping the question', () => {
    const question = `  **why** ${'x'.repeat(2200)}\0\uFFFD\uD800😀  `;
    expect(
      composePdfQuestion(
        question,
        quotePdfSelection('first\nsecond\0\uFFFD\uDC00')
      )
    ).toBe(`> first\n> second\n\n  **why** ${'x'.repeat(2200)}😀  `);
    const splitSurrogate = quotePdfSelection(
      `${'a'.repeat(996)}😀${'b'.repeat(1200)}`
    );
    expect(composePdfQuestion('why', splitSurrogate)).toBe(
      `> ${'a'.repeat(996)}...${'b'.repeat(997)}\n\nwhy`
    );
  });
});
