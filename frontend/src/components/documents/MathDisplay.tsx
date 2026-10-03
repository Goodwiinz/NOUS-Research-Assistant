'use client';

import React, { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';
import DOMPurify from 'dompurify';

interface MathDisplayProps {
  content: string;
  format?: 'latex' | 'markdown';
  block?: boolean;
}

export const MathDisplay: React.FC<MathDisplayProps> = ({
  content,
  format = 'latex',
  block = false,
}) => {
  const [katexHtml, setKatexHtml] = useState<string | null>(null);
  const [katexLoaded, setKatexLoaded] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (format !== 'latex') return;

    let cancelled = false;

    const loadKatex = async () => {
      try {
        const katex = (await import('katex')).default;
        await import('katex/dist/katex.min.css');

        if (cancelled) return;

        const html = katex.renderToString(content, {
          throwOnError: false,
          displayMode: block,
        });

        // KaTeX output is injected via dangerouslySetInnerHTML, so sanitize it.
        // Use profiles only: DOMPurify ignores ALLOWED_TAGS when USE_PROFILES
        // is set. KaTeX needs all three namespaces: html (span), mathMl
        // (accessibility tree, matrices) and svg (radicals, stretchy arrows,
        // braces, \\cancel). Without svg those glyphs silently disappear.
        const safeHtml = DOMPurify.sanitize(html, {
          USE_PROFILES: { html: true, svg: true, mathMl: true },
        });

        setKatexHtml(safeHtml);
        setKatexLoaded(true);
      } catch {
        if (!cancelled) {
          setKatexLoaded(false);
          // Fall back to the raw-LaTeX error branch below; `false` here kept
          // that branch dead and rendered nothing on failure (R6-L14).
          setError(true);
        }
      }
    };

    loadKatex();
    return () => {
      cancelled = true;
    };
  }, [content, format, block]);

  if (format === 'markdown') {
    return (
      <pre
        className={cn(
          'font-mono text-sm text-muted-foreground whitespace-pre-wrap',
          block
            ? 'my-3 rounded-lg border border-border bg-muted p-4'
            : 'inline rounded bg-black/30 px-1.5 py-0.5'
        )}
      >
        {content}
      </pre>
    );
  }

  if (katexLoaded && katexHtml) {
    return (
      <span
        className={cn(block && 'my-3 flex justify-center')}
        dangerouslySetInnerHTML={{ __html: katexHtml }}
      />
    );
  }

  if (error) {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1.5',
          block && 'my-3 flex justify-center'
        )}
      >
        <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-helios" />
        <code
          className={cn(
            'font-mono text-sm text-muted-foreground',
            'rounded border border-border bg-muted px-2 py-1',
            'bg-linear-to-r from-primary/5 to-muted'
          )}
        >
          {content}
        </code>
      </span>
    );
  }

  return (
    <code
      className={cn(
        'font-mono text-sm text-muted-foreground',
        'rounded border border-border bg-muted px-2 py-1',
        'bg-linear-to-r from-primary/5 to-muted',
        block && 'my-3 block text-center'
      )}
    >
      {content}
    </code>
  );
};

export default MathDisplay;
