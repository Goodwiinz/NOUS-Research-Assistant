import { describe, expect, it } from 'vitest';

import {
  parseHighlightedSnippet,
  stripHighlightTags,
} from '../searchHighlight';

describe('parseHighlightedSnippet', () => {
  it('splits marked terms into highlighted segments', () => {
    expect(
      parseHighlightedSnippet(
        'exactly seven <mark>sample</mark> <mark>sensors</mark>: Cedar-01'
      )
    ).toEqual([
      { text: 'exactly seven ', highlighted: false },
      { text: 'sample', highlighted: true },
      { text: ' ', highlighted: false },
      { text: 'sensors', highlighted: true },
      { text: ': Cedar-01', highlighted: false },
    ]);
  });

  it('returns one plain segment when there are no tags', () => {
    expect(parseHighlightedSnippet('plain text')).toEqual([
      { text: 'plain text', highlighted: false },
    ]);
    expect(parseHighlightedSnippet('')).toEqual([]);
    expect(parseHighlightedSnippet(undefined)).toEqual([]);
  });

  it('treats tags case-insensitively', () => {
    expect(parseHighlightedSnippet('a <MARK>b</Mark> c')).toEqual([
      { text: 'a ', highlighted: false },
      { text: 'b', highlighted: true },
      { text: ' c', highlighted: false },
    ]);
  });

  it('keeps nested marks highlighted until the outer one closes', () => {
    expect(
      parseHighlightedSnippet('<mark>a <mark>b</mark> c</mark> d')
    ).toEqual([
      { text: 'a b c', highlighted: true },
      { text: ' d', highlighted: false },
    ]);
  });

  it('highlights an unclosed mark to the end and drops a stray close', () => {
    expect(parseHighlightedSnippet('x</mark> y <mark>z')).toEqual([
      { text: 'x y ', highlighted: false },
      { text: 'z', highlighted: true },
    ]);
  });

  it('leaves other tags and entities as literal text', () => {
    expect(
      parseHighlightedSnippet('<b>bold</b> &amp; <mark>hit</mark> &lt;x&gt;')
    ).toEqual([
      { text: '<b>bold</b> &amp; ', highlighted: false },
      { text: 'hit', highlighted: true },
      { text: ' &lt;x&gt;', highlighted: false },
    ]);
  });
});

describe('stripHighlightTags', () => {
  it('removes only mark tags, any case, and keeps entities', () => {
    expect(
      stripHighlightTags('<mark>a</mark> <MARK>b</MARK> <b>c</b> &amp;')
    ).toBe('a b <b>c</b> &amp;');
  });

  it('passes through nullish values', () => {
    expect(stripHighlightTags(undefined)).toBeUndefined();
    expect(stripHighlightTags(null)).toBeUndefined();
  });
});
