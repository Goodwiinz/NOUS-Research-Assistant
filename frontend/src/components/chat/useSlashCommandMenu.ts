import { useCallback, useMemo, useState } from 'react';

import {
  filterCommands,
  isSlashTrigger,
  SLASH_COMMANDS,
  type SlashCommand,
} from './slashCommands';

export interface SlashCommandMenuState {
  /** Whether the menu should be shown for the current input value. */
  isOpen: boolean;
  /** Commands matching the current "/query". */
  filtered: SlashCommand[];
  /** Index into `filtered` of the keyboard-highlighted row. */
  highlightedIndex: number;
  setHighlightedIndex: (index: number) => void;
  /** Move the highlight by `delta`, wrapping around the list. */
  move: (delta: number) => void;
  /** Hide the menu without clearing the typed token; re-arms on next keystroke. */
  dismiss: () => void;
  /** Open the menu without replacing the current draft. */
  open: () => void;
  /** True when the menu was opened over a non-slash draft. */
  isManual: boolean;
}

/**
 * Derives the slash-command menu state from the composer's textarea value,
 * with an explicit-open path for the Commands button that never replaces the
 * current draft.
 *
 * The menu opens only while the entire value is a bare "/word" token
 * (`isSlashTrigger`), and the highlight resets to the top whenever the query
 * changes so it can never point past the filtered list.
 */
export function useSlashCommandMenu(value: string): SlashCommandMenuState {
  // Escape sets `dismissed` to hide the menu without erasing the typed token;
  // any new keystroke (value change) re-arms it below.
  const [dismissed, setDismissed] = useState(false);
  const [isManual, setIsManual] = useState(false);
  const isTriggered = isSlashTrigger(value);
  const isOpen = (isTriggered || isManual) && !dismissed;

  const filtered = useMemo(
    () =>
      isOpen ? (isTriggered ? filterCommands(value) : SLASH_COMMANDS) : [],
    [isOpen, isTriggered, value]
  );

  const [highlightedIndex, setHighlightedIndex] = useState(0);

  const [previousValue, setPreviousValue] = useState(value);
  if (value !== previousValue) {
    setPreviousValue(value);
    setHighlightedIndex(0);
    setDismissed(false);
    // A manually opened menu is a one-shot overlay. Once the user edits the
    // draft, let the normal typed-slash trigger decide whether it stays open.
    setIsManual(false);
  }

  const move = useCallback(
    (delta: number) => {
      setHighlightedIndex((current) => {
        const n = filtered.length;
        if (n === 0) return 0;
        return (current + delta + n) % n;
      });
    },
    [filtered.length]
  );

  const dismiss = useCallback(() => {
    setDismissed(true);
    setIsManual(false);
  }, []);

  const open = useCallback(() => {
    setDismissed(false);
    setIsManual(true);
  }, []);

  return {
    isOpen,
    filtered,
    highlightedIndex,
    setHighlightedIndex,
    move,
    dismiss,
    open,
    isManual,
  };
}
