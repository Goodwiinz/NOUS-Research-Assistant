import * as p from '@clack/prompts';
import { loadConfig, saveConfig } from './auth/store';
import { parseSlashCommand } from './hooks/useSlashCommands';
import { streamAgent, streamConfirm } from './stream';
import { confirmKey, isConfirmCancel } from './confirmKey';
import {
  fetchThreadMessages,
  loadThreadMessagesWithCache,
  fetchThreads,
  type RemoteThreadSummary,
} from './services/threads';
import {
  derivePreview,
  deriveTitle,
  getThread,
  listThreads,
  reconcileThreads,
  removeThread,
  touchThread,
  upsertThread,
  type ThreadEntry,
} from './services/threadStore';
import { fetchProjects, type RemoteProjectSummary } from './services/projects';
import {
  fetchDocuments,
  fetchProjectDocuments,
  type RemoteDocumentSummary,
} from './services/documents';
import { countVisualRows, hasMarkdown, renderMarkdown } from './markdown';
import { buildCompleter, isPromptCancel, readPrompt } from './prompt';
import { readDraft, clearDraft } from './services/draft';
import { classifyError } from './errors';
import { withRetry } from './retry';
import { c, glyph } from './theme';
import {
  ResponseWriter,
  thinkingLineOut,
  toolBadgePrefix,
  turnSeparator,
} from './output';

interface ActiveProject {
  id: string;
  name: string;
}

interface ActivePaper {
  id: string;
  title: string;
}

interface ReplOptions {
  projectId?: string;
  projectName?: string;
  paperId?: string;
  paperTitle?: string;
}

// Drafts shorter than this (after trim) are treated as artifacts and not
// restored on the next launch — a stray escape sequence, an empty line, or
// a single keypress isn't worth replaying as a prefilled prompt.
export const MIN_DRAFT_RESTORE_CHARS = 2;

export async function runRepl(options: ReplOptions = {}): Promise<void> {
  const config = loadConfig();
  if (!config) throw new Error('Not logged in');

  let threadId = config.thread_id ?? null;
  let activeProject: ActiveProject | null =
    options.projectId && options.projectName
      ? { id: options.projectId, name: options.projectName }
      : config.project_id && config.project_name
        ? { id: config.project_id, name: config.project_name }
        : null;
  let activePaper: ActivePaper | null =
    options.paperId && options.paperTitle
      ? { id: options.paperId, title: options.paperTitle }
      : config.paper_id && config.paper_title
        ? { id: config.paper_id, title: config.paper_title }
        : null;

  p.intro(
    `${c.boldCyan('NOUS')} ${c.gray('·')} ${c.dim(formatStatus(threadId, activeProject, activePaper))}`
  );

  let pendingDraft: string | null = null;
  const initialDraft = readDraft();
  if (initialDraft && initialDraft.trim().length >= MIN_DRAFT_RESTORE_CHARS) {
    pendingDraft = initialDraft;
    clearDraft();
  } else if (initialDraft) {
    // Tiny drafts (<2 chars) are almost always artifacts — a stray escape
    // sequence or single keypress. Drop them silently so they don't get
    // replayed as a prefilled prompt on the next launch.
    clearDraft();
  }

  let activeAbort: AbortController | null = null;
  let lastUserMessage: string | null = null;
  const onSigint = () => {
    if (activeAbort) {
      activeAbort.abort();
      return; // mid-stream → just cancel, don't exit
    }
    p.outro('Bye.');
    process.exit(0);
  };
  process.on('SIGINT', onSigint);

  try {
    while (true) {
      const completer = buildCompleter({
        knownThreadIds: listThreads().map((t) => t.id),
      });
      const initialValue = pendingDraft ?? undefined;
      pendingDraft = null;
      const raw = await readPrompt({ message: '>', completer, initialValue });
      if (isPromptCancel(raw) || p.isCancel(raw)) {
        p.outro('Bye.');
        break;
      }

      const input = (raw as string).trim();
      if (!input) continue;

      const parsed = parseSlashCommand(input);
      if (parsed) {
        const done = await handleSlashCommand(parsed.command, parsed.args, {
          threadId,
          activeProject,
          activePaper,
          onThreadChange: (t) => {
            threadId = t;
          },
          onProjectChange: (proj) => {
            activeProject = proj;
            persistContext({ project: proj, paper: activePaper });
          },
          onPaperChange: (paper) => {
            activePaper = paper;
            persistContext({ project: activeProject, paper });
          },
          onExit: () => {
            p.outro('Bye.');
            process.exit(0);
          },
          lastUserMessage,
          retryLast: async () => {
            if (!lastUserMessage) {
              p.log.warn('Nothing to retry.');
              return;
            }
            const retryCtx = buildPageContext(activeProject, activePaper);
            activeAbort = new AbortController();
            await streamToTerminal(
              lastUserMessage,
              retryCtx,
              activeAbort.signal
            );
            activeAbort = null;
          },
        });
        if (done) continue;
        // Unknown slash command — don't forward "/foo" to the agent as a
        // user message. Print a hint and re-prompt.
        p.log.warn(`Unknown command: /${parsed.command}. Try /help.`);
        continue;
      }

      const previousThreadId = threadId;
      const ctx = buildPageContext(activeProject, activePaper);

      activeAbort = new AbortController();
      await streamToTerminal(input, ctx, activeAbort.signal);
      activeAbort = null;
      lastUserMessage = input;

      const updated = loadConfig();
      if (updated && updated.thread_id !== threadId) {
        threadId = updated.thread_id ?? null;
      }

      if (threadId) {
        recordThreadUse(threadId, previousThreadId, input, activeProject);
      }

      renderFooter(threadId, activeProject, activePaper);
    }
  } finally {
    process.off('SIGINT', onSigint);
  }
}

export async function runOneShot(
  query: string,
  pageContext: Record<string, unknown> = {}
): Promise<void> {
  await streamToTerminal(query, pageContext);
}

function formatStatus(
  threadId: string | null,
  activeProject: ActiveProject | null,
  activePaper: ActivePaper | null
): string {
  const parts: string[] = [];
  if (!threadId) {
    parts.push('no thread');
  } else {
    const entry = getThread(threadId);
    const short = threadId.slice(0, 8);
    parts.push(
      entry ? `thread: ${short} · "${entry.title}"` : `thread: ${short}`
    );
  }
  if (activeProject) parts.push(`proj: ${activeProject.name}`);
  if (activePaper) parts.push(`paper: ${truncate(activePaper.title, 40)}`);
  return parts.join('  ·  ');
}

function buildPageContext(
  activeProject: ActiveProject | null,
  activePaper: ActivePaper | null
): Record<string, unknown> {
  const ctx: Record<string, unknown> = activeProject
    ? {
        type: 'project',
        project_id: activeProject.id,
        project_name: activeProject.name,
      }
    : { type: 'chat' };
  if (activePaper) {
    ctx.paper_id = activePaper.id;
    ctx.paper_title = activePaper.title;
  }
  return ctx;
}

function persistContext(state: {
  project: ActiveProject | null;
  paper: ActivePaper | null;
}): void {
  const cfg = loadConfig();
  if (!cfg) return;
  saveConfig({
    ...cfg,
    project_id: state.project?.id ?? null,
    project_name: state.project?.name ?? null,
    paper_id: state.paper?.id ?? null,
    paper_title: state.paper?.title ?? null,
  });
}

function renderFooter(
  threadId: string | null,
  activeProject: ActiveProject | null,
  activePaper: ActivePaper | null
): void {
  const status = formatStatus(threadId, activeProject, activePaper);
  const cols =
    process.stdout.isTTY && process.stdout.columns
      ? process.stdout.columns
      : 80;
  const visiblePrefix = `╰─ ${status} `;
  const padLen = Math.max(0, cols - visiblePrefix.length);
  const rule = '─'.repeat(padLen);
  process.stdout.write(`${c.gray('╰─')} ${c.dim(status)} ${c.gray(rule)}\n`);
}

function recordThreadUse(
  threadId: string,
  previousThreadId: string | null,
  userInput: string,
  activeProject: ActiveProject | null
): void {
  const isNewThread = previousThreadId !== threadId;
  const existing = getThread(threadId);
  if (isNewThread || !existing) {
    upsertThread({
      id: threadId,
      title: deriveTitle(userInput),
      preview: derivePreview(userInput),
      project_id: activeProject?.id ?? null,
      project_name: activeProject?.name ?? null,
    });
  } else {
    upsertThread({
      id: threadId,
      preview: derivePreview(userInput),
    });
  }
}

async function streamToTerminal(
  message: string,
  pageContext: Record<string, unknown>,
  signal?: AbortSignal
): Promise<void> {
  const spinners = new Map<string, ReturnType<typeof p.spinner>>();
  const collectedContexts: Array<Record<string, unknown>> = [];
  let lastUsage: {
    inputTokens: number;
    outputTokens: number;
    costUsd: number | null;
  } | null = null;
  let writer = new ResponseWriter();
  let retriedAfterMissingThread = false;
  let confirmRetried = false;
  let activeConfirmThreadId: string | null = null;

  turnSeparator();

  type EventStream = AsyncGenerator<import('./stream').StreamEvent>;
  let current: EventStream;
  try {
    current = await withRetry(
      () => Promise.resolve(streamAgent(message, pageContext, { signal })),
      {
        maxAttempts: 2,
        signal: signal ?? new AbortController().signal,
        predicate: (e) => classifyError(e).retryable,
      }
    );
  } catch (e) {
    process.stdout.write('\n');
    const classified = classifyError(e);
    p.log.error(classified.userMessage);
    if (classified.hint) p.log.message(`  ${classified.hint}`);
    return;
  }

  while (true) {
    let pendingConfirmThreadId: string | null = null;
    let retryFreshThread = false;
    let retryConfirm = false;

    try {
      for await (const event of current) {
        if (event.type === 'token') {
          writer.writeToken(event.content);
        } else if (event.type === 'tool_start') {
          const s = p.spinner();
          s.start(
            `${toolBadgePrefix()} ${formatToolLine(event.tool, event.args)}`
          );
          spinners.set(event.tool, s);
        } else if (event.type === 'tool_end') {
          const label = formatToolEndLine(
            event.tool,
            event.result,
            event.isError
          );
          const s = spinners.get(event.tool);
          const decorated = `${toolBadgePrefix()} ${label}`;
          if (s) {
            if (event.isError) {
              s.error(decorated);
            } else {
              s.stop(decorated);
            }
            spinners.delete(event.tool);
          } else if (event.isError) {
            p.log.error(`${c.red(glyph.cross)} ${label}`);
          } else {
            p.log.success(`${c.green(glyph.check)} ${label}`);
          }
        } else if (event.type === 'plan') {
          if (event.steps.length > 0) {
            thinkingLineOut(
              `${c.magenta(glyph.spark)} ${c.bold('Plan')}`,
              event.steps.join(`  ${c.gray('→')}  `)
            );
          }
        } else if (event.type === 'reflection') {
          // A revising reflection means the agent is discarding the draft it
          // just streamed and regenerating (backend: severity=major, round<2).
          // Close the flawed draft block BEFORE the marker, then start a fresh
          // writer so the revised answer doesn't concatenate onto the draft —
          // the webapp clears streamingContent on the same `revising` signal.
          if (event.revising) writer.abort();
          if (event.passed) {
            thinkingLineOut(
              `${c.green(glyph.check)} ${c.bold(`Reflection #${event.round}`)}`,
              c.dim('passed')
            );
          } else {
            const detail =
              event.issues.length > 0 ? event.issues.join('; ') : 'failed';
            thinkingLineOut(
              `${c.yellow(glyph.warn)} ${c.bold(`Reflection #${event.round}`)}`,
              c.yellow(detail)
            );
          }
          if (event.revising) writer = new ResponseWriter();
        } else if (event.type === 'rag_context') {
          if (event.contexts.length > 0) {
            collectedContexts.push(...event.contexts);
            thinkingLineOut(
              `${c.blue(glyph.bullet)} ${c.bold('Context')}`,
              c.dim(`retrieved ${event.contexts.length} chunk(s)`)
            );
          }
        } else if (event.type === 'usage') {
          lastUsage = {
            inputTokens: event.inputTokens,
            outputTokens: event.outputTokens,
            costUsd: event.costUsd,
          };
        } else if (event.type === 'confirmation') {
          for (const [tool, s] of spinners) {
            s.cancel(`${tool} (paused — awaiting confirmation)`);
          }
          spinners.clear();
          writer.abort();
          process.stdout.write('\n');
          renderConfirmationDetails(event.details);
          const ok = await confirmKey({
            message: confirmationPromptMessage(event.details),
            default: true,
          });
          if (isConfirmCancel(ok) || p.isCancel(ok) || !ok) {
            p.log.warn('Cancelled.');
            return;
          }
          p.log.info('Resuming agent…');
          pendingConfirmThreadId = event.threadId;
          activeConfirmThreadId = event.threadId;
          break;
        } else if (event.type === 'done') {
          writer.finish();
          renderCitationsFooter(collectedContexts);
          renderUsageLine(lastUsage);
          process.stdout.write('\n');
          return;
        } else if (event.type === 'error') {
          writer.abort();
          const classified = classifyError(event.message);
          if (
            classified.kind === 'not_found_thread' &&
            activeConfirmThreadId &&
            !confirmRetried
          ) {
            // Confirm flow failed — retry the confirm once before giving up.
            // The backend reconnects its checkpointer on retry.
            confirmRetried = true;
            retryConfirm = true;
            p.log.warn('Connection hiccup — retrying confirmation…');
            break;
          }
          if (
            classified.kind === 'not_found_thread' &&
            !retriedAfterMissingThread
          ) {
            clearCachedThreadId();
            retriedAfterMissingThread = true;
            retryFreshThread = true;
            p.log.warn(
              `${classified.userMessage} ${classified.hint ?? ''}`.trim()
            );
            break;
          }
          p.log.error(classified.userMessage);
          if (classified.hint) p.log.message(`  ${classified.hint}`);
          return;
        }
      }
    } catch (e) {
      const classified = classifyError(e);
      if (classified.kind === 'cancelled') {
        writer.abort();
        p.log.warn('Cancelled.');
        return;
      }
      throw e;
    }

    if (retryConfirm && activeConfirmThreadId) {
      writer = new ResponseWriter();
      current = streamConfirm(activeConfirmThreadId, true, { signal });
      continue;
    }

    if (retryFreshThread) {
      writer = new ResponseWriter();
      current = streamAgent(message, pageContext, { signal });
      continue;
    }

    if (pendingConfirmThreadId === null) break;

    // Reset the response block between pre- and post-confirm halves so the
    // post-confirm finish doesn't include the LLM's pre-confirm preamble.
    writer = new ResponseWriter();
    current = streamConfirm(pendingConfirmThreadId, true, { signal });
  }
}

function clearCachedThreadId(): void {
  const cfg = loadConfig();
  if (cfg && cfg.thread_id !== null) {
    saveConfig({ ...cfg, thread_id: null });
  }
}

function formatToolLine(tool: string, args: string): string {
  const compact = compactArgs(args);
  return compact ? `${tool} ${compact}` : tool;
}

const MD_INPLACE_MAX_ROWS = 200;
const MD_DISABLED = process.env.NOUS_MD === '0';

export function maybeReformatMarkdown(buffer: string): void {
  if (MD_DISABLED) return;
  if (!buffer || !hasMarkdown(buffer)) return;
  const cols = process.stdout.columns ?? 80;
  // We already wrote a trailing '\n' on done, so the cursor is one row
  // below the last visible line of the buffer. Account for that.
  const rows = countVisualRows(buffer, cols) + 1;
  if (rows > MD_INPLACE_MAX_ROWS) return;
  if (!process.stdout.isTTY) return;
  process.stdout.write(`\x1b[${rows}F\x1b[J`);
  process.stdout.write(renderMarkdown(buffer));
  if (!buffer.endsWith('\n')) process.stdout.write('\n');
}

function compactArgs(raw: string): string {
  if (!raw) return '';
  const cleaned = raw.replace(/\s+/g, ' ').trim();
  if (cleaned.length === 0) return '';
  const max = 80;
  return cleaned.length > max ? `${cleaned.slice(0, max - 1)}…` : cleaned;
}

const RESULT_PREVIEW_MAX = 80;
const KNOWN_PLURAL_KEYS = [
  'papers',
  'projects',
  'documents',
  'notes',
  'drafts',
] as const;

function tryParseJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function compactPreview(text: string): string {
  const cleaned = text.replace(/\s+/g, ' ').trim();
  if (cleaned.length <= RESULT_PREVIEW_MAX) return cleaned;
  return `${cleaned.slice(0, RESULT_PREVIEW_MAX - 1)}…`;
}

export function pickSummary(parsed: Record<string, unknown>): string | null {
  if (typeof parsed.summary === 'string' && parsed.summary.length > 0) {
    return parsed.summary;
  }

  const ingested = parsed.documents_ingested;
  const paperIds = parsed.paper_ids;
  if (typeof ingested === 'number' && Array.isArray(paperIds)) {
    return `${ingested} of ${paperIds.length} ingested`;
  }

  if (typeof parsed.total === 'number') {
    for (const key of KNOWN_PLURAL_KEYS) {
      if (Array.isArray(parsed[key])) {
        return `${parsed.total} ${key}`;
      }
    }
  }

  for (const [key, value] of Object.entries(parsed)) {
    if (key.endsWith('_id') && typeof value === 'string') {
      const name =
        typeof parsed.name === 'string' && parsed.name.length > 0
          ? parsed.name
          : value.slice(0, 8);
      return `created ${name}`;
    }
  }

  return null;
}

export function formatToolEndLine(
  tool: string,
  result: string,
  isError: boolean
): string {
  const parsed = tryParseJson(result);
  const parsedObj =
    parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;

  // Errors win
  if (isError || (parsedObj && typeof parsedObj.error === 'string')) {
    const msg =
      parsedObj && typeof parsedObj.error === 'string'
        ? parsedObj.error
        : compactPreview(result);
    return `${tool} ${compactPreview(msg)}`;
  }

  // Empty / no useful payload
  if (!result || result === '{}' || result === '[]') {
    return tool;
  }

  // Structured success
  if (parsedObj) {
    const summary = pickSummary(parsedObj);
    return summary ? `${tool} · ${summary}` : tool;
  }

  // String success
  return `${tool} · ${compactPreview(result)}`;
}

export function renderCitationsFooter(
  contexts: Array<Record<string, unknown>>
): void {
  if (contexts.length === 0) return;
  const seen = new Set<string>();
  const unique: Array<Record<string, unknown>> = [];
  for (const ctx of contexts) {
    const key = `${ctx.document_id ?? ''}::${ctx.title ?? ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(ctx);
    if (unique.length >= 5) break;
  }
  if (unique.length === 0) return;

  p.log.message('— Citations —');
  unique.forEach((ctx, idx) => {
    const title =
      typeof ctx.title === 'string' && ctx.title ? ctx.title : 'Untitled';
    const docId =
      typeof ctx.document_id === 'string' ? ctx.document_id.slice(0, 8) : null;
    const score = typeof ctx.score === 'number' ? ctx.score.toFixed(2) : null;
    const meta = [
      docId ? `doc ${docId}` : null,
      score ? `score ${score}` : null,
    ]
      .filter(Boolean)
      .join(' · ');
    const suffix = meta ? `  (${meta})` : '';
    p.log.message(`  [${idx + 1}] ${title}${suffix}`);
  });
}

export function renderUsageLine(
  usage: {
    inputTokens: number;
    outputTokens: number;
    costUsd: number | null;
  } | null
): void {
  if (!usage) return;
  if (usage.inputTokens === 0 && usage.outputTokens === 0) return;
  const cost =
    usage.costUsd !== null && usage.costUsd > 0
      ? ` · $${usage.costUsd.toFixed(4)}`
      : '';
  p.log.message(`→ ${usage.inputTokens} in / ${usage.outputTokens} out${cost}`);
}

interface SlashContext {
  threadId: string | null;
  activeProject: ActiveProject | null;
  activePaper: ActivePaper | null;
  onThreadChange: (t: string | null) => void;
  onProjectChange: (proj: ActiveProject | null) => void;
  onPaperChange: (paper: ActivePaper | null) => void;
  onExit: () => void;
  lastUserMessage: string | null;
  retryLast: () => Promise<void>;
}

async function handleSlashCommand(
  command: string,
  args: string[],
  ctx: SlashContext
): Promise<boolean> {
  if (command === 'quit') {
    ctx.onExit();
    return true;
  }
  if (command === 'new') {
    const cfg = loadConfig();
    if (cfg) saveConfig({ ...cfg, thread_id: null });
    ctx.onThreadChange(null);
    p.log.success('New thread started.');
    return true;
  }
  if (command === 'thread') {
    if (!ctx.threadId) {
      p.log.info('Thread: none');
      return true;
    }
    const entry = getThread(ctx.threadId);
    if (entry) {
      p.log.info(`Thread: ${ctx.threadId}`);
      p.log.message(`  title: ${entry.title}`);
      if (entry.project_name) p.log.message(`  project: ${entry.project_name}`);
      p.log.message(`  last used: ${formatRelative(entry.last_used_at)}`);
    } else {
      p.log.info(`Thread: ${ctx.threadId}`);
    }
    return true;
  }
  if (command === 'threads') {
    await handleThreadsCommand(ctx);
    return true;
  }
  if (command === 'history') {
    await handleHistoryCommand(ctx, args);
    return true;
  }
  if (command === 'forget') {
    handleForgetCommand(ctx, args);
    return true;
  }
  if (command === 'projects') {
    await handleProjectsCommand(ctx);
    return true;
  }
  if (command === 'papers' || command === 'paper') {
    await handlePapersCommand(ctx);
    return true;
  }
  if (command === 'context') {
    if (args[0] === 'clear') {
      ctx.onProjectChange(null);
      ctx.onPaperChange(null);
      p.log.success('Project + paper context cleared.');
      return true;
    }
    if (args[0] === 'project' && args[1]) {
      ctx.onProjectChange({ id: args[1], name: args[2] ?? args[1] });
      p.log.success(`Project set: ${args[2] ?? args[1]}`);
      return true;
    }
    if (args[0] === 'paper' && args[1]) {
      const id = args[1];
      const title = args.slice(2).join(' ') || id;
      ctx.onPaperChange({ id, title });
      p.log.success(`Paper set: ${title}`);
      return true;
    }
  }
  if (command === 'settings') {
    await handleSettingsCommand(args);
    return true;
  }
  if (command === 'retry') {
    await ctx.retryLast();
    return true;
  }
  if (command === 'clear') {
    // Clear the visible scrollback; keep the active thread + project.
    // Respect TTY: in non-TTY contexts (tests, pipes) just print a
    // separator so the intent is visible but no escape sequences leak.
    if (process.stdout.isTTY) {
      // ESC[2J = erase entire screen, ESC[H = cursor home
      process.stdout.write('\x1b[2J\x1b[H');
    } else {
      p.log.message('— cleared —');
    }
    return true;
  }
  if (command === 'help') {
    p.log.message(
      '/new  /clear  /thread  /threads  /history [n]  /forget [id]  /projects  /papers  /retry  /context project <id> [name]  /context paper <id> [title]  /context clear  /settings [set api_url <url>]  /quit'
    );
    return true;
  }
  return false;
}

async function handleProjectsCommand(ctx: SlashContext): Promise<void> {
  const spinner = p.spinner();
  spinner.start('Loading projects');
  let projects: RemoteProjectSummary[] = [];
  try {
    projects = await fetchProjects();
    spinner.stop(`Loaded ${projects.length} project(s)`);
  } catch (err) {
    spinner.stop('Failed to load projects');
    p.log.error((err as Error).message);
    return;
  }

  if (projects.length === 0) {
    p.log.info('No projects yet. Create one in the web app first.');
    return;
  }

  const sorted = [...projects].sort((a, b) =>
    (b.updated_at || '').localeCompare(a.updated_at || '')
  );

  const choice = await p.select({
    message: 'Pick a project (esc to cancel)',
    options: [
      { value: '__clear__', label: '× Clear project context' },
      ...sorted.map((proj) => ({
        value: proj.id,
        label: formatProjectLabel(proj, ctx.activeProject?.id ?? null),
        hint: projectHint(proj),
      })),
      { value: '__cancel__', label: '(cancel)' },
    ],
  });

  if (p.isCancel(choice) || choice === '__cancel__') return;

  if (choice === '__clear__') {
    ctx.onProjectChange(null);
    p.log.success('Project context cleared.');
    return;
  }

  const picked = sorted.find((p) => p.id === choice);
  if (!picked) return;
  ctx.onProjectChange({ id: picked.id, name: picked.name });
  p.log.success(`Project set: ${picked.name}`);
}

function formatProjectLabel(
  proj: RemoteProjectSummary,
  activeId: string | null
): string {
  const marker = proj.id === activeId ? '● ' : '  ';
  const when = proj.updated_at ? formatRelative(proj.updated_at) : '';
  const name = truncate(proj.name, 50);
  return when ? `${marker}${name} · ${when}` : `${marker}${name}`;
}

function projectHint(proj: RemoteProjectSummary): string | undefined {
  const parts: string[] = [`#${proj.id.slice(0, 8)}`];
  if (proj.document_count > 0) parts.push(`${proj.document_count} docs`);
  if (proj.note_count > 0) parts.push(`${proj.note_count} notes`);
  if (proj.draft_count > 0) parts.push(`${proj.draft_count} drafts`);
  return parts.join(' · ');
}

async function handlePapersCommand(ctx: SlashContext): Promise<void> {
  const spinner = p.spinner();
  const scoped = ctx.activeProject;
  spinner.start(scoped ? `Loading papers in ${scoped.name}` : 'Loading papers');
  let documents: RemoteDocumentSummary[] = [];
  try {
    documents = scoped
      ? await fetchProjectDocuments(scoped.id)
      : await fetchDocuments({ limit: 50 });
    spinner.stop(`Loaded ${documents.length} paper(s)`);
  } catch (err) {
    spinner.stop('Failed to load papers');
    p.log.error((err as Error).message);
    return;
  }

  if (documents.length === 0) {
    p.log.info(
      scoped
        ? `No papers in ${scoped.name}. Add one via the web app or /context clear to browse all.`
        : 'No documents yet. Ingest one first.'
    );
    return;
  }

  const sorted = [...documents].sort((a, b) =>
    (b.updated_at || '').localeCompare(a.updated_at || '')
  );

  const choice = await p.select({
    message: 'Pick a paper (esc to cancel)',
    options: [
      { value: '__clear__', label: '× Clear paper context' },
      ...sorted.map((doc) => ({
        value: doc.id,
        label: formatPaperLabel(doc, ctx.activePaper?.id ?? null),
        hint: paperHint(doc),
      })),
      { value: '__cancel__', label: '(cancel)' },
    ],
  });

  if (p.isCancel(choice) || choice === '__cancel__') return;

  if (choice === '__clear__') {
    ctx.onPaperChange(null);
    p.log.success('Paper context cleared.');
    return;
  }

  const picked = sorted.find((d) => d.id === choice);
  if (!picked) return;
  ctx.onPaperChange({ id: picked.id, title: picked.title });
  p.log.success(`Paper set: ${picked.title}`);
}

function formatPaperLabel(
  doc: RemoteDocumentSummary,
  activeId: string | null
): string {
  const marker = doc.id === activeId ? '● ' : '  ';
  const when = doc.updated_at ? formatRelative(doc.updated_at) : '';
  const title = truncate(doc.title || doc.filename || doc.id, 60);
  return when ? `${marker}${title} · ${when}` : `${marker}${title}`;
}

function paperHint(doc: RemoteDocumentSummary): string | undefined {
  const parts: string[] = [`#${doc.id.slice(0, 8)}`];
  if (doc.document_type) parts.push(doc.document_type);
  if (doc.processing_status && doc.processing_status !== 'completed') {
    parts.push(doc.processing_status);
  }
  return parts.join(' · ');
}

async function handleThreadsCommand(ctx: SlashContext): Promise<void> {
  const spinner = p.spinner();
  spinner.start('Loading threads');
  let remote: RemoteThreadSummary[] = [];
  try {
    remote = await fetchThreads();
    spinner.stop(`Loaded ${remote.length} thread(s)`);
  } catch (err) {
    spinner.stop('Failed to load threads from server');
    p.log.warn((err as Error).message);
  }

  const local = listThreads();
  const merged = mergeThreads(local, remote);

  if (remote.length > 0) {
    const remoteIds = remote.map((t) => t.id);
    reconcileThreads(remoteIds);
    if (ctx.threadId && !remoteIds.includes(ctx.threadId)) {
      const cfg = loadConfig();
      if (cfg && cfg.thread_id === ctx.threadId) {
        saveConfig({ ...cfg, thread_id: null });
      }
      ctx.onThreadChange(null);
    }
  }

  if (merged.length === 0) {
    p.log.info('No threads yet. Send a message to start one.');
    return;
  }

  const choice = await p.select({
    message: 'Pick a thread (esc to cancel)',
    options: [
      ...merged.map((entry) => ({
        value: entry.id,
        label: formatThreadLabel(entry, ctx.threadId),
        hint: threadHint(entry),
      })),
      { value: '__cancel__', label: '(cancel)' },
    ],
  });

  if (p.isCancel(choice) || choice === '__cancel__') return;

  const picked = merged.find((e) => e.id === choice);
  if (!picked) return;

  const cfg = loadConfig();
  if (cfg) saveConfig({ ...cfg, thread_id: picked.id });
  touchThread(picked.id);
  ctx.onThreadChange(picked.id);
  p.log.success(`Switched to "${picked.title}" (${picked.id.slice(0, 8)})`);

  try {
    const remoteMeta = remote.find((r) => r.id === picked.id) ?? null;
    const view = await loadThreadMessagesWithCache(
      picked.id,
      remoteMeta?.last_message_at ?? null
    );
    const conversation = view.messages.filter(
      (m) => m.role.toLowerCase() !== 'tool'
    );
    if (conversation.length > 0) {
      const tail = conversation.slice(-6);
      const omitted = conversation.length - tail.length;
      p.log.message('');
      if (omitted > 0) {
        p.log.message(
          `  ╭─ ${omitted} earlier message${omitted === 1 ? '' : 's'} ─╮`
        );
      }
      for (const msg of tail) {
        renderPreviewMessage({
          role: msg.role,
          content: msg.content,
          created_at:
            'created_at' in msg
              ? (msg as { created_at: string }).created_at
              : (msg as { ended_at: string }).ended_at,
        });
      }
      p.log.message('  ╰───────────────────────╯');
    }
  } catch {
    // History preview is best-effort — don't block the switch.
  }
}

async function handleHistoryCommand(
  ctx: SlashContext,
  args: string[]
): Promise<void> {
  if (!ctx.threadId) {
    p.log.warn('No active thread. Use /threads to pick one.');
    return;
  }
  const limit = parseLimit(args[0], 10);

  const spinner = p.spinner();
  spinner.start('Loading history');
  try {
    const messages = await fetchThreadMessages(ctx.threadId);
    spinner.stop(`${messages.length} message(s)`);

    if (messages.length === 0) {
      p.log.info('No messages in this thread.');
      return;
    }
    const tail = messages.slice(-limit);
    for (const msg of tail) {
      renderHistoryMessage(msg);
    }
  } catch (err) {
    spinner.stop('Failed to load history');
    p.log.error((err as Error).message);
  }
}

function handleForgetCommand(ctx: SlashContext, args: string[]): void {
  const target = args[0] ?? ctx.threadId;
  if (!target) {
    p.log.warn('No thread to forget. Pass an id or have an active thread.');
    return;
  }
  const removed = removeThread(target);
  if (!removed) {
    p.log.warn(`No local entry for ${target.slice(0, 8)}.`);
  } else {
    p.log.success(`Forgot ${target.slice(0, 8)} (server copy untouched).`);
  }
  if (ctx.threadId === target) {
    const cfg = loadConfig();
    if (cfg) saveConfig({ ...cfg, thread_id: null });
    ctx.onThreadChange(null);
  }
}

const EDITABLE_SETTINGS = ['api_url', 'model'] as const;
type EditableSetting = (typeof EDITABLE_SETTINGS)[number];

const SUPPORTED_MODELS = [
  { value: '', label: '(server default)' },
  { value: 'model-router', label: 'model-router  — auto-route' },
  { value: 'gpt-4o', label: 'gpt-4o' },
  { value: 'gpt-4o-mini', label: 'gpt-4o-mini' },
  { value: 'gpt-5', label: 'gpt-5' },
  { value: 'gpt-5-mini', label: 'gpt-5-mini' },
  { value: 'gpt-5-nano', label: 'gpt-5-nano' },
  { value: 'gpt-5-chat', label: 'gpt-5-chat' },
  { value: 'gpt-5.2', label: 'gpt-5.2' },
  { value: 'gpt-5.2-chat', label: 'gpt-5.2-chat' },
  { value: 'o4-mini', label: 'o4-mini' },
  { value: 'claude-sonnet-4-5', label: 'claude-sonnet-4-5' },
  { value: 'claude-haiku-4-5', label: 'claude-haiku-4-5' },
];

async function handleSettingsCommand(args: string[]): Promise<void> {
  const cfg = loadConfig();
  if (!cfg) {
    p.log.error('Not logged in.');
    return;
  }

  if (args[0] === 'set') {
    const key = args[1] as EditableSetting | undefined;
    const value = args[2];
    if (!key || value === undefined) {
      p.log.warn('Usage: /settings set <api_url|model> <value>');
      return;
    }
    if (!EDITABLE_SETTINGS.includes(key)) {
      p.log.warn(
        `Unknown setting "${key}". Editable: ${EDITABLE_SETTINGS.join(', ')}`
      );
      return;
    }
    if (key === 'model') {
      const valid = SUPPORTED_MODELS.map((m) => m.value);
      if (!valid.includes(value)) {
        p.log.warn(
          `Unknown model "${value}". Supported: ${valid.filter(Boolean).join(', ')}`
        );
        return;
      }
    }
    saveConfig({ ...cfg, [key]: value });
    p.log.success(`${key} → ${value || '(server default)'}`);
    return;
  }

  const modelLabel =
    SUPPORTED_MODELS.find((m) => m.value === (cfg.model ?? ''))?.label ??
    cfg.model ??
    '(server default)';
  p.log.message('Current settings:');
  p.log.message(`  email      ${cfg.user_email}`);
  p.log.message(`  org        ${cfg.organization_id}`);
  p.log.message(`  api_url    ${cfg.api_url ?? '(default)'}`);
  p.log.message(`  model      ${modelLabel}`);
  const expiry = formatRelative(cfg.expires_at);
  p.log.message(`  token      expires ${expiry}`);
  p.log.message(`  config     ~/.nous/config.json`);

  if (args[0] === undefined) {
    const action = await p.select({
      message: 'Edit a setting?',
      options: [
        { value: 'model', label: 'model — deployment to use' },
        { value: 'api_url', label: 'api_url — backend URL' },
        { value: '__done__', label: '(done)' },
      ],
    });
    if (p.isCancel(action) || action === '__done__') return;

    if (action === 'model') {
      const next = await p.select({
        message: 'Select model',
        options: SUPPORTED_MODELS.map((m) => ({
          ...m,
          label:
            m.value === (cfg.model ?? '') ? `● ${m.label}` : `  ${m.label}`,
        })),
      });
      if (p.isCancel(next)) return;
      saveConfig({ ...cfg, model: next as string });
      const label =
        SUPPORTED_MODELS.find((m) => m.value === next)?.label ?? String(next);
      p.log.success(`model → ${label}`);
      return;
    }

    const current = cfg[action as EditableSetting] ?? '';
    const next = await p.text({
      message: `New value for ${action as string}`,
      initialValue: current,
      placeholder: 'https://api.example.com',
    });
    if (p.isCancel(next) || !next) return;
    saveConfig({ ...cfg, [action as string]: next as string });
    p.log.success(`${action as string} → ${next as string}`);
  }
}

function mergeThreads(
  local: ThreadEntry[],
  remote: RemoteThreadSummary[]
): ThreadEntry[] {
  const byId = new Map<string, ThreadEntry>();
  for (const entry of local) byId.set(entry.id, entry);

  for (const r of remote) {
    const existing = byId.get(r.id);
    const titleCandidate = r.title ?? existing?.title ?? '(untitled)';
    const lastUsed =
      r.last_message_at ?? r.updated_at ?? existing?.last_used_at ?? '';
    byId.set(r.id, {
      id: r.id,
      title: titleCandidate || '(untitled)',
      project_id: r.source_project_id ?? existing?.project_id ?? null,
      project_name: existing?.project_name ?? null,
      last_used_at: lastUsed,
      preview: existing?.preview ?? '',
    });
  }

  return Array.from(byId.values()).sort((a, b) =>
    b.last_used_at.localeCompare(a.last_used_at)
  );
}

function threadHint(entry: ThreadEntry): string | undefined {
  const parts: string[] = [`#${entry.id.slice(0, 8)}`];
  if (entry.preview) parts.push(truncate(entry.preview, 60));
  return parts.join(' · ');
}

function formatThreadLabel(
  entry: ThreadEntry,
  activeId: string | null
): string {
  const marker = entry.id === activeId ? '● ' : '  ';
  const when = formatRelative(entry.last_used_at);
  const title = truncate(entry.title || entry.id, 50);
  return `${marker}${title} · ${when}`;
}

function formatRelative(iso: string): string {
  if (!iso) return 'unknown';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const diffMs = Date.now() - then;
  // Clock skew or future timestamps → treat as "just now" instead of negative.
  if (diffMs < 0) return 'just now';
  const sec = Math.round(diffMs / 1000);
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ago`;
  return new Date(then).toISOString().slice(0, 10);
}

function parseLimit(arg: string | undefined, fallback: number): number {
  if (!arg) return fallback;
  const n = Number.parseInt(arg, 10);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(n, 100);
}

function renderHistoryMessage(msg: {
  role: string;
  content: string;
  tool_name: string | null;
  created_at: string;
}): void {
  const when = formatRelative(msg.created_at);
  const role = msg.role.toLowerCase();
  if (role === 'tool') {
    const label = msg.tool_name ?? 'tool';
    p.log.message(`[${when}] ⚙ ${label}: ${truncate(msg.content, 200)}`);
    return;
  }
  if (role === 'user') {
    p.log.message(`[${when}] ▸ you: ${truncate(msg.content, 400)}`);
    return;
  }
  p.log.message(`[${when}] ◂ agent: ${truncate(msg.content, 400)}`);
}

const PREVIEW_AGENT_LINES = 3;

function renderPreviewMessage(msg: {
  role: string;
  content: string;
  created_at: string;
}): void {
  const when = formatRelative(msg.created_at);
  const role = msg.role.toLowerCase();
  const isUser = role === 'user';
  const prefix = isUser ? '\x1b[36m▸ you\x1b[0m' : '\x1b[33m◂ nous\x1b[0m';
  const cols = process.stdout.columns ?? 80;
  const lineMax = Math.min(cols - 14, 140);

  if (isUser) {
    p.log.message(
      `  ${prefix}  ${truncate(msg.content, lineMax)}  \x1b[2m${when}\x1b[0m`
    );
    return;
  }

  const cleaned = msg.content.replace(/\s+/g, ' ').trim();
  const lines: string[] = [];
  let remaining = cleaned;
  for (let i = 0; i < PREVIEW_AGENT_LINES && remaining.length > 0; i++) {
    if (remaining.length <= lineMax) {
      lines.push(remaining);
      remaining = '';
    } else {
      lines.push(`${remaining.slice(0, lineMax - 1)}…`);
      remaining = remaining.slice(lineMax - 1);
    }
  }
  if (remaining.length > 0 && lines.length > 0) {
    const last = lines[lines.length - 1];
    if (!last.endsWith('…')) {
      lines[lines.length - 1] = `${last.slice(0, -1)}…`;
    }
  }

  const pad = '         ';
  lines.forEach((line, i) => {
    if (i === 0) {
      p.log.message(`  ${prefix}  ${line}  \x1b[2m${when}\x1b[0m`);
    } else {
      p.log.message(`  ${pad}${line}`);
    }
  });
}

function truncate(text: string, max: number): string {
  const cleaned = text.replace(/\s+/g, ' ').trim();
  if (cleaned.length <= max) return cleaned;
  return `${cleaned.slice(0, max - 1)}…`;
}

const TOOL_LABELS: Record<string, string> = {
  ingest_arxiv_papers: 'Download & index arXiv paper(s)',
  add_document_to_project: 'Add document to project',
  create_project: 'Create new project',
  create_project_note: 'Save note to project',
  create_draft: 'Generate draft document',
  revise_draft: 'Revise saved draft',
  execute_code: 'Execute code',
};

interface PendingTool {
  name: string;
  args: Record<string, unknown>;
}

function extractPendingTools(details: Record<string, unknown>): PendingTool[] {
  const raw = details.tools;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(
      (t): t is PendingTool =>
        typeof t === 'object' && t !== null && 'name' in t
    )
    .map((t) => ({
      name: String((t as { name: unknown }).name ?? ''),
      args:
        typeof (t as { args?: unknown }).args === 'object' &&
        (t as { args?: unknown }).args !== null
          ? (t as { args: Record<string, unknown> }).args
          : {},
    }));
}

// Tool arg fields whose value is the user-meaningful body of the action
// (e.g. the note text, the code to run). These render as a preview block
// in the confirmation panel rather than getting truncated to ~60 chars
// inside summarizeArgs — otherwise a 5k-token LLM-drafted note collapses
// to "content=The paper proposes…" and the user approves blind.
const CONTENT_ARG_FIELDS = new Set(['content', 'code', 'body', 'text', 'note']);
const CONTENT_PREVIEW_MIN_CHARS = 60;

function isPreviewableContent(key: string, value: unknown): value is string {
  return (
    CONTENT_ARG_FIELDS.has(key) &&
    typeof value === 'string' &&
    value.length > CONTENT_PREVIEW_MIN_CHARS
  );
}

function summarizeArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args).filter(
    ([k, v]) => !isPreviewableContent(k, v)
  );
  if (entries.length === 0) return '';
  return entries
    .map(([k, v]) => {
      let val: string;
      if (Array.isArray(v)) {
        val = v.length <= 3 ? `[${v.join(', ')}]` : `[${v.length} items]`;
      } else if (typeof v === 'object' && v !== null) {
        val = '{…}';
      } else {
        // Collapse newlines/tabs/other control whitespace to single spaces
        // BEFORE truncating, so a multi-line short string doesn't break the
        // confirmation panel into multiple visual rows (which makes the
        // following args render as continuation of this value).
        const s = String(v).replace(/\s+/g, ' ').trim();
        val = s.length > 60 ? `${s.slice(0, 57)}…` : s;
      }
      return `${k}=${val}`;
    })
    .join('  ');
}

interface ContentPreview {
  field: string;
  totalChars: number;
  preview: string;
  truncated: boolean;
}

const CONTENT_PREVIEW_MAX_LINES = 10;
const CONTENT_PREVIEW_MAX_CHARS = 800;

export function buildContentPreviews(
  args: Record<string, unknown>
): ContentPreview[] {
  const out: ContentPreview[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (!isPreviewableContent(k, v)) continue;
    const lines = v.split('\n');
    const taken: string[] = [];
    let chars = 0;
    let truncated = false;
    for (const line of lines) {
      if (taken.length >= CONTENT_PREVIEW_MAX_LINES) {
        truncated = true;
        break;
      }
      const remaining = CONTENT_PREVIEW_MAX_CHARS - chars;
      if (line.length > remaining) {
        taken.push(`${line.slice(0, Math.max(0, remaining - 1))}…`);
        truncated = true;
        break;
      }
      taken.push(line);
      chars += line.length + 1;
    }
    out.push({
      field: k,
      totalChars: v.length,
      preview: taken.join('\n'),
      truncated: truncated || taken.join('\n').length < v.length,
    });
  }
  return out;
}

export function renderConfirmationDetails(
  details: Record<string, unknown>
): void {
  const tools = extractPendingTools(details);
  const message = typeof details.message === 'string' ? details.message : '';

  p.log.warn('⚠  Confirmation required');
  if (tools.length === 0) {
    if (message) p.log.message(message);
    else
      p.log.message('The agent is paused and needs your approval to continue.');
    p.log.message('Reply  y  to allow,  n  to cancel.');
    return;
  }

  p.log.message(
    `The agent wants to run ${tools.length} action${tools.length === 1 ? '' : 's'} that will modify your data:`
  );
  for (const t of tools) {
    const label = TOOL_LABELS[t.name] ?? t.name;
    const argSummary = summarizeArgs(t.args);
    p.log.message(
      `  • ${label}  (${t.name})${argSummary ? `\n      ${argSummary}` : ''}`
    );
    for (const cp of buildContentPreviews(t.args)) {
      p.log.message(`      ${cp.field} (${cp.totalChars} chars):`);
      for (const line of cp.preview.split('\n')) {
        p.log.message(`        │ ${line}`);
      }
      if (cp.truncated) {
        const omitted = cp.totalChars - cp.preview.length;
        p.log.message(
          `        │ … (truncated, ${omitted} more char${omitted === 1 ? '' : 's'})`
        );
      }
    }
  }
  p.log.message('Reply  y  to allow,  n  to cancel.');
}

export function confirmationPromptMessage(
  details: Record<string, unknown>
): string {
  const tools = extractPendingTools(details);
  if (tools.length === 0) return 'Allow the agent to continue?';
  if (tools.length === 1) {
    const label = TOOL_LABELS[tools[0].name] ?? tools[0].name;
    return `Allow: ${label}?`;
  }
  return `Allow these ${tools.length} actions?`;
}
