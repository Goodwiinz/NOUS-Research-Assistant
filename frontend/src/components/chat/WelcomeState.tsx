'use client';

import { motion, useReducedMotion } from 'framer-motion';
import {
  CornerDownLeft,
  GitCompare,
  ListChecks,
  Quote,
  Workflow,
} from 'lucide-react';
import React from 'react';

export interface WelcomeStateProps {
  onPromptSelect: (prompt: string) => void;
}

interface Starter {
  icon: typeof ListChecks;
  prompt: string;
}

// Researcher tasks, not feature boasts. Each populates the composer verbatim.
const STARTERS: Starter[] = [
  {
    icon: ListChecks,
    prompt: 'Summarize the document or passage I provide in three points',
  },
  { icon: Quote, prompt: 'Show the sources behind this claim: ' },
  { icon: Workflow, prompt: 'How do these entities relate? Context: ' },
  { icon: GitCompare, prompt: 'Compare these findings: ' },
];

const NOUS_EASE: [number, number, number, number] = [0.16, 1, 0.3, 1];

export function WelcomeState({ onPromptSelect }: WelcomeStateProps) {
  const reduceMotion = useReducedMotion();

  return (
    <div className="flex min-h-full flex-col items-center justify-center px-6 py-16">
      <motion.div
        initial={reduceMotion ? false : { opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: NOUS_EASE }}
        className="w-full max-w-(--nous-chat-col)"
      >
        <h2
          className="text-[1.625rem] font-semibold leading-tight tracking-[-0.02em] text-(--nous-fg-1)"
          style={{ fontFamily: 'var(--nous-font-heading)' }}
        >
          What would you like to find out?
        </h2>
        <p
          className="mt-3 max-w-[46ch] text-[0.9375rem] leading-relaxed text-(--nous-fg-2)"
          style={{ fontFamily: 'var(--nous-font-body)' }}
        >
          Ask a question and NOUS answers from your corpus, tracing every claim
          back to the source passage it came from.
        </p>

        <p
          className="mt-9 mb-2.5 text-xs font-medium text-(--nous-fg-3)"
          style={{ fontFamily: 'var(--nous-font-ui)' }}
        >
          Start with
        </p>

        <ul className="overflow-hidden rounded-(--nous-radius-lg) border border-(--nous-border-1) bg-(--nous-bg-2)">
          {STARTERS.map(({ icon: Icon, prompt }, idx) => (
            <li key={prompt}>
              <button
                type="button"
                onClick={() => onPromptSelect(prompt)}
                className="group flex w-full items-center gap-3.5 px-3.5 py-3 text-left transition-colors duration-150 hover:bg-(--nous-sol-subtle) focus-visible:bg-(--nous-sol-subtle) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40 focus-visible:ring-inset"
                style={{
                  borderTop:
                    idx === 0 ? undefined : '1px solid var(--nous-border-1)',
                }}
              >
                <span
                  aria-hidden
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-(--nous-radius-md) bg-(--nous-bg-3) text-(--nous-fg-3) transition-colors duration-150 group-hover:text-(--nous-sol) group-focus-visible:text-(--nous-sol)"
                >
                  <Icon className="h-4 w-4" strokeWidth={1.8} />
                </span>
                <span
                  className="flex-1 text-[0.9375rem] text-(--nous-fg-1)"
                  style={{ fontFamily: 'var(--nous-font-ui)' }}
                >
                  {prompt}
                </span>
                <CornerDownLeft
                  aria-hidden
                  className="h-4 w-4 shrink-0 text-(--nous-fg-3) opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100"
                  strokeWidth={1.8}
                />
              </button>
            </li>
          ))}
        </ul>

        <p
          className="mt-2 text-xs text-(--nous-fg-3)"
          style={{ fontFamily: 'var(--nous-font-ui)' }}
        >
          Templates are editable — add the document, passage, claim, or findings
          before sending.
        </p>

        <p
          className="mt-4 text-xs text-(--nous-fg-3)"
          style={{ fontFamily: 'var(--nous-font-ui)' }}
        >
          Press Enter to send, Shift + Enter for a new line.
        </p>
      </motion.div>
    </div>
  );
}

export default WelcomeState;
