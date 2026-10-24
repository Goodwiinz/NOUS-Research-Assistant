/**
 * Slash-command registry for the chat composer.
 *
 * Mirrors the command names the user already has in the terminal app
 * (`cli/hooks/useSlashCommands.ts`) so the web chat and the terminal share
 * one vocabulary. Running a command prints its result into the chat transcript
 * (see `commandOutput.ts` / `CommandOutputBubble`), CLI-style; nothing
 * navigates away.
 *
 * Kept JSX- and dependency-free so it can be imported anywhere (component,
 * hook, test) without pulling in React.
 */

export type SlashCommandId =
  | 'new'
  | 'retry'
  | 'clear'
  | 'threads'
  | 'projects'
  | 'papers'
  | 'summarize'
  | 'keypoints'
  | 'gaps'
  | 'timeline'
  | 'remember'
  | 'memories'
  | 'help';

export interface SlashCommand {
  id: SlashCommandId;
  /** The "/name" rendered in the menu. */
  label: string;
  /** One-line description shown beside the label. */
  title: string;
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { id: 'new', label: '/new', title: 'Start a new chat' },
  { id: 'retry', label: '/retry', title: 'Regenerate the last response' },
  { id: 'clear', label: '/clear', title: 'Clear command output' },
  { id: 'threads', label: '/threads', title: 'Browse and switch threads' },
  { id: 'projects', label: '/projects', title: 'Set the project context' },
  { id: 'papers', label: '/papers', title: 'Browse your papers' },
  { id: 'summarize', label: '/summarize', title: 'Summarize your sources' },
  { id: 'keypoints', label: '/keypoints', title: 'Key points with citations' },
  { id: 'gaps', label: '/gaps', title: 'Gaps and open questions' },
  { id: 'timeline', label: '/timeline', title: 'Timeline from your sources' },
  {
    id: 'remember',
    label: '/remember',
    title: 'Save a fact to project memory',
  },
  { id: 'memories', label: '/memories', title: 'View project memory' },
  { id: 'help', label: '/help', title: 'List commands' },
];

/**
 * True when the whole input is a bare slash-command token — `/`, `/n`,
 * `/proj` — i.e. a single "/word" with no spaces. The menu opens only in this
 * state, which is never a sendable message, so it can never interfere with
 * normal send-on-Enter.
 */
export function isSlashTrigger(value: string): boolean {
  return /^\/[a-z]*$/i.test(value);
}

/** Commands matching the current "/query" (by id prefix or label substring). */
export function filterCommands(value: string): SlashCommand[] {
  const q = value.replace(/^\//, '').toLowerCase();
  if (!q) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter(
    (c) => c.id.startsWith(q) || c.label.toLowerCase().includes(q)
  );
}
