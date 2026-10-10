/**
 * @vitest-environment node
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import type { Mock, Mocked, MockedFunction } from 'vitest';
import { mkdtempSync, rmSync } from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as prompts from '@clack/prompts';
import {
  buildContentPreviews,
  confirmationPromptMessage,
  formatToolEndLine,
  pickSummary,
  renderCitationsFooter,
  renderConfirmationDetails,
  renderUsageLine,
  runRepl,
} from '../repl';
import * as store from '../auth/store';
import * as stream from '../stream';
import * as threadsService from '../services/threads';
import * as projectsService from '../services/projects';

vi.mock('@clack/prompts', () => ({
  text: vi.fn(),
  confirm: vi.fn(),
  select: vi.fn(),
  isCancel: vi.fn((value) => value === '__CANCEL__'),
  intro: vi.fn(),
  outro: vi.fn(),
  spinner: vi.fn(() => ({
    start: vi.fn(),
    stop: vi.fn(),
    error: vi.fn(),
    cancel: vi.fn(),
  })),
  log: {
    error: vi.fn(),
    info: vi.fn(),
    message: vi.fn(),
    success: vi.fn(),
    warn: vi.fn(),
  },
}));

vi.mock('../auth/store');
vi.mock('../stream');
vi.mock('../services/threads', () => ({
  fetchThreads: vi.fn(),
  fetchThreadMessages: vi.fn(),
}));
vi.mock('../services/projects', () => ({
  fetchProjects: vi.fn(),
}));

let tmpConfigDir: string;

const mockedText = prompts.text as MockedFunction<typeof prompts.text>;
const mockedConfirm = prompts.confirm as MockedFunction<typeof prompts.confirm>;
const mockedLog = prompts.log as Mocked<typeof prompts.log>;
const mockedLoadConfig = store.loadConfig as MockedFunction<
  typeof store.loadConfig
>;
const mockedSaveConfig = store.saveConfig as MockedFunction<
  typeof store.saveConfig
>;
const mockedStreamAgent = stream.streamAgent as MockedFunction<
  typeof stream.streamAgent
>;
const mockedStreamConfirm = stream.streamConfirm as MockedFunction<
  typeof stream.streamConfirm
>;

const CONFIG: store.NousConfig = {
  token: 'tok_test',
  user_email: 'a@b.com',
  organization_id: 'org_1',
  expires_at: '2099-01-01T00:00:00Z',
  thread_id: 'stale-thread',
};

async function* events(items: stream.StreamEvent[]) {
  for (const item of items) yield item;
}

beforeEach(() => {
  tmpConfigDir = mkdtempSync(path.join(os.tmpdir(), 'nous-repl-test-'));
  process.env.NOUS_CONFIG_DIR = tmpConfigDir;
  let config = { ...CONFIG };
  mockedLoadConfig.mockReset();
  mockedSaveConfig.mockReset();
  mockedText.mockReset();
  mockedConfirm.mockReset();
  mockedStreamAgent.mockReset();
  mockedStreamConfirm.mockReset();
  mockedLog.error.mockReset();
  mockedLog.info.mockReset();
  mockedLog.message.mockReset();
  mockedLog.success.mockReset();
  mockedLog.warn.mockReset();
  mockedLoadConfig.mockImplementation(() => config);
  mockedSaveConfig.mockImplementation((next) => {
    config = next;
  });
  mockedText
    .mockResolvedValueOnce('hi' as never)
    .mockResolvedValueOnce('__CANCEL__' as never);
  mockedConfirm.mockResolvedValue(true as never);
  vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
});

afterEach(() => {
  vi.restoreAllMocks();
  rmSync(tmpConfigDir, { recursive: true, force: true });
  delete process.env.NOUS_CONFIG_DIR;
});

test('clears a missing confirmation thread and retries the message once', async () => {
  mockedStreamAgent
    .mockReturnValueOnce(
      events([
        {
          type: 'confirmation',
          threadId: 'stale-thread',
          details: { approval_id: 'a'.repeat(64), message: 'Confirm?' },
        },
      ])
    )
    .mockReturnValueOnce(
      events([{ type: 'token', content: 'fresh response' }, { type: 'done' }])
    );
  mockedStreamConfirm.mockImplementation(() =>
    events([{ type: 'error', message: 'Thread not found' }])
  );

  await runRepl();

  // First confirm + one retry confirm (both fail), then fresh thread retry
  expect(mockedStreamConfirm).toHaveBeenCalledTimes(2);
  expect(mockedStreamConfirm).toHaveBeenCalledWith(
    'stale-thread',
    true,
    expect.any(Object)
  );
  expect(mockedSaveConfig).toHaveBeenCalledWith(
    expect.objectContaining({ thread_id: null })
  );
  expect(mockedStreamAgent).toHaveBeenCalledTimes(2);
  expect(mockedStreamAgent).toHaveBeenNthCalledWith(
    2,
    'hi',
    { type: 'chat' },
    expect.any(Object)
  );
  expect(mockedLog.warn).toHaveBeenCalledWith(
    expect.stringContaining('Cached thread expired')
  );
});

test('retries confirm once before falling back to fresh thread', async () => {
  mockedStreamAgent.mockReturnValueOnce(
    events([
      {
        type: 'confirmation',
        threadId: 'stale-thread',
        details: { approval_id: 'a'.repeat(64), message: 'Confirm?' },
      },
    ])
  );
  // First confirm attempt fails, second succeeds
  mockedStreamConfirm
    .mockReturnValueOnce(
      events([{ type: 'error', message: 'Thread not found' }])
    )
    .mockReturnValueOnce(
      events([{ type: 'token', content: 'ingested' }, { type: 'done' }])
    );

  await runRepl();

  // streamConfirm should have been called twice (retry)
  expect(mockedStreamConfirm).toHaveBeenCalledTimes(2);
  // Both calls should use the same thread_id
  expect(mockedStreamConfirm).toHaveBeenNthCalledWith(
    1,
    'stale-thread',
    true,
    expect.any(Object)
  );
  expect(mockedStreamConfirm).toHaveBeenNthCalledWith(
    2,
    'stale-thread',
    true,
    expect.any(Object)
  );
  // Should NOT have retried the full message
  expect(mockedStreamAgent).toHaveBeenCalledTimes(1);
  // Should show the retry message, not "Cached thread expired"
  expect(mockedLog.warn).toHaveBeenCalledWith(
    expect.stringContaining('retrying confirmation')
  );
});

test('falls through to fresh thread if confirm retry also fails', async () => {
  mockedStreamAgent
    .mockReturnValueOnce(
      events([
        {
          type: 'confirmation',
          threadId: 'stale-thread',
          details: { approval_id: 'a'.repeat(64), message: 'Confirm?' },
        },
      ])
    )
    .mockReturnValueOnce(
      events([{ type: 'token', content: 'fresh' }, { type: 'done' }])
    );
  // Both confirm attempts fail
  mockedStreamConfirm
    .mockReturnValueOnce(
      events([{ type: 'error', message: 'Thread not found' }])
    )
    .mockReturnValueOnce(
      events([{ type: 'error', message: 'Thread not found' }])
    );

  await runRepl();

  // Confirm was retried once
  expect(mockedStreamConfirm).toHaveBeenCalledTimes(2);
  // Then fell through to full message retry
  expect(mockedStreamAgent).toHaveBeenCalledTimes(2);
  expect(mockedLog.warn).toHaveBeenCalledWith(
    expect.stringContaining('Cached thread expired')
  );
});

describe('confirmation rendering', () => {
  test('lists each pending destructive tool with a friendly label', () => {
    renderConfirmationDetails({
      tools: [
        {
          name: 'ingest_arxiv_papers',
          args: { arxiv_ids: ['2310.11522'] },
        },
        {
          name: 'create_draft',
          args: {
            title: 'My draft',
            source_document_ids: ['a', 'b', 'c', 'd'],
          },
        },
      ],
      message: 'Confirm?',
    });

    expect(mockedLog.warn).toHaveBeenCalledWith('⚠  Confirmation required');
    const messages = mockedLog.message.mock.calls.map((c) => c[0]);
    expect(
      messages.some((m) => /will modify your data/.test(m as string))
    ).toBe(true);
    expect(
      messages.some((m) => /Download & index arXiv/.test(m as string))
    ).toBe(true);
    expect(
      messages.some((m) => /Generate draft document/.test(m as string))
    ).toBe(true);
    expect(messages.some((m) => /\[4 items\]/.test(m as string))).toBe(true);
    expect(messages.some((m) => /Reply  y  to allow/.test(m as string))).toBe(
      true
    );
  });

  test('falls back to backend message when no tools are sent', () => {
    renderConfirmationDetails({ message: 'Confirm: ingest_arxiv_papers?' });
    const messages = mockedLog.message.mock.calls.map((c) => c[0]);
    expect(messages).toContain('Confirm: ingest_arxiv_papers?');
  });

  test('prompt summarizes single vs multi tool', () => {
    expect(
      confirmationPromptMessage({
        tools: [{ name: 'create_project', args: {} }],
      })
    ).toBe('Allow: Create new project?');
    expect(
      confirmationPromptMessage({
        tools: [
          { name: 'create_project', args: {} },
          { name: 'create_draft', args: {} },
        ],
      })
    ).toBe('Allow these 2 actions?');
    expect(confirmationPromptMessage({})).toBe('Allow the agent to continue?');
  });

  test('renders long content arg as a preview block instead of truncating', () => {
    const longContent = Array.from(
      { length: 12 },
      (_, i) => `Paragraph ${i + 1}: this is a non-trivial body of text.`
    ).join('\n');

    renderConfirmationDetails({
      tools: [
        {
          name: 'create_project_note',
          args: {
            project_id: 'ce83e229-3f5f-45b3-81cb-6bfccf0ba252',
            title: 'Modular supercuspidal lifts',
            content: longContent,
          },
        },
      ],
    });

    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    // Inline summary keeps short fields and SUPPRESSES the long content
    // field so the user isn't approving 5k tokens of "content=Paragraph 1:…".
    const inlineSummary = messages.find(
      (m) => m.includes('project_id=') && m.includes('title=')
    );
    expect(inlineSummary).toBeDefined();
    expect(inlineSummary).not.toMatch(/content=/);

    // Block-rendered content with chars header + at least one preview line.
    expect(messages.some((m) => /content \(\d+ chars\):/.test(m))).toBe(true);
    expect(
      messages.some((m) => /Paragraph 1: this is a non-trivial/.test(m))
    ).toBe(true);
    expect(messages.some((m) => /truncated, \d+ more chars?/.test(m))).toBe(
      true
    );
  });

  test('does NOT preview short content fields — keeps them inline', () => {
    renderConfirmationDetails({
      tools: [
        {
          name: 'create_project_note',
          args: { title: 't', content: 'short body' },
        },
      ],
    });
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => m.includes('content=short body'))).toBe(true);
    expect(messages.some((m) => /content \(\d+ chars\):/.test(m))).toBe(false);
  });

  test('summarizeArgs collapses newlines/control whitespace in short string values', () => {
    // Below CONTENT_PREVIEW_MIN_CHARS (60) so it stays in the inline summary
    // path rather than being rendered as a content preview block. Without the
    // fix, the embedded \n would break the panel into multiple visual rows
    // and make trailing args look like continuation of `summary`.
    renderConfirmationDetails({
      tools: [
        {
          name: 'create_draft',
          args: { title: 't', summary: 'line1\nline2 line3' },
        },
      ],
    });
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    const inline = messages.find(
      (m) => m.includes('title=') && m.includes('summary=')
    );
    expect(inline).toBeDefined();
    expect(inline).toContain('summary=line1 line2 line3');
    // A literal newline inside the summarized arg value would split the line.
    const summaryFragment = (inline as string).slice(
      (inline as string).indexOf('summary=')
    );
    expect(summaryFragment).not.toContain('\n');
  });

  test('summarizeArgs still truncates long single-line strings with an ellipsis', () => {
    // 70-char single-line value, no newlines. Must be > 60 chars (truncation
    // threshold) AND short enough to stay below CONTENT_PREVIEW_MIN_CHARS so
    // it doesn't get diverted into the preview block path.
    const longSingleLine = 'a'.repeat(70);
    expect(longSingleLine.length).toBeGreaterThan(60);
    renderConfirmationDetails({
      tools: [
        {
          name: 'create_draft',
          args: { title: longSingleLine },
        },
      ],
    });
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    const inline = messages.find((m) => m.includes('title='));
    expect(inline).toBeDefined();
    // Truncated to 57 chars + ellipsis = 58 visible chars after `title=`.
    expect(inline).toMatch(/title=a{57}…/);
  });
});

describe('buildContentPreviews', () => {
  test('extracts only previewable content fields above the threshold', () => {
    const previews = buildContentPreviews({
      content: 'a'.repeat(200),
      title: 'irrelevant short value',
      project_id: 'p-1',
      code: 'tiny',
    });
    expect(previews).toHaveLength(1);
    expect(previews[0].field).toBe('content');
    expect(previews[0].totalChars).toBe(200);
    expect(previews[0].truncated).toBe(false);
  });

  test('caps preview at line and char limits and reports truncation', () => {
    const longLine = 'x'.repeat(2000);
    const [preview] = buildContentPreviews({ content: longLine });
    expect(preview.truncated).toBe(true);
    expect(preview.preview.length).toBeLessThan(longLine.length);
    expect(preview.totalChars).toBe(2000);
  });

  test('ignores non-string and non-content fields', () => {
    expect(
      buildContentPreviews({
        content: 12345,
        meta: { content: 'a'.repeat(200) },
      })
    ).toEqual([]);
  });
});

describe('confirm flow happy path', () => {
  test('NEVER prints "Actions completed." after PR-D-1 — per-tool summaries replace it', async () => {
    mockedStreamAgent.mockReturnValueOnce(
      events([
        {
          type: 'confirmation',
          threadId: 'thread-1',
          details: {
            approval_id: 'a'.repeat(64),
            tools: [{ name: 'create_draft', args: {} }],
          },
        },
      ])
    );
    mockedStreamConfirm.mockReturnValueOnce(events([{ type: 'done' }]));

    await runRepl();

    expect(mockedStreamConfirm).toHaveBeenCalledWith(
      'thread-1',
      true,
      expect.any(Object)
    );
    const successCalls = mockedLog.success.mock.calls.map(
      (c) => c[0] as string
    );
    expect(successCalls.every((m) => !/Actions completed/i.test(m))).toBe(true);
  });

  test('suppresses "Actions completed." when LLM emits tokens after confirmation', async () => {
    mockedStreamAgent.mockReturnValueOnce(
      events([
        {
          type: 'confirmation',
          threadId: 'thread-2',
          details: {
            approval_id: 'a'.repeat(64),
            tools: [{ name: 'ingest_arxiv_papers', args: {} }],
          },
        },
      ])
    );
    mockedStreamConfirm.mockReturnValueOnce(
      events([
        { type: 'token', content: 'Done — completed: ingest_arxiv_papers.' },
        { type: 'done' },
      ])
    );

    await runRepl();

    const successCalls = mockedLog.success.mock.calls.map(
      (c) => c[0] as string
    );
    expect(successCalls.every((m) => !m.includes('Actions completed.'))).toBe(
      true
    );
  });

  test('does not carry pre-confirm tokens into post-confirm markdown reformat', async () => {
    const writeSpy = vi
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true);
    Object.defineProperty(process.stdout, 'isTTY', {
      value: true,
      configurable: true,
    });
    Object.defineProperty(process.stdout, 'columns', {
      value: 80,
      configurable: true,
    });

    const markdownBlock = '# Heading\n\n- item one\n- item two\n- item three\n';
    mockedStreamAgent.mockReturnValueOnce(
      events([
        { type: 'token', content: markdownBlock },
        {
          type: 'confirmation',
          threadId: 'thread-3',
          details: {
            approval_id: 'a'.repeat(64),
            tools: [{ name: 'create_project_note', args: {} }],
          },
        },
      ])
    );
    mockedStreamConfirm.mockReturnValueOnce(events([{ type: 'done' }]));

    await runRepl();

    // With the fix applied, tokenBuffer is reset before the confirm stream.
    // maybeReformatMarkdown receives an empty buffer and exits early — no
    // ANSI cursor-up escape should be written after the confirm done event.
    const allWrites = writeSpy.mock.calls.map((c) => String(c[0]));
    const ansiMoveUp = allWrites.filter((s) => /\x1b\[\d+F/.test(s));
    expect(ansiMoveUp).toHaveLength(0);

    Object.defineProperty(process.stdout, 'isTTY', {
      value: undefined,
      configurable: true,
    });
    Object.defineProperty(process.stdout, 'columns', {
      value: undefined,
      configurable: true,
    });
  });
});

describe('thread registry side effects', () => {
  test('records the user prompt as a thread title in the local registry', async () => {
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('Summarise the latest arXiv on RAG' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    mockedStreamAgent.mockReturnValueOnce(
      events([{ type: 'token', content: 'ok' }, { type: 'done' }])
    );

    await runRepl();

    const reg = (await import('../services/threadStore')).loadRegistry();
    expect(reg.entries).toHaveLength(1);
    expect(reg.entries[0].id).toBe('stale-thread');
    expect(reg.entries[0].title).toBe('Summarise the latest arXiv on RAG');
  });
});

describe('slash commands: threads/history/forget', () => {
  const fetchThreadsMock = threadsService.fetchThreads as unknown as Mock;
  const fetchMessagesMock =
    threadsService.fetchThreadMessages as unknown as Mock;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mockedSelect = (prompts as any).select as Mock;

  beforeEach(() => {
    fetchThreadsMock.mockReset();
    fetchMessagesMock.mockReset();
    mockedSelect.mockReset();
  });

  test('/threads switches to the picked thread', async () => {
    const store = await import('../services/threadStore');
    store.upsertThread({ id: 'thread-A', title: 'Older' });
    store.upsertThread({ id: 'thread-B', title: 'Newer' });

    fetchThreadsMock.mockResolvedValueOnce([]);
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/threads' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    mockedSelect.mockResolvedValueOnce('thread-A' as never);

    await runRepl();

    expect(fetchThreadsMock).toHaveBeenCalled();
    expect(mockedSaveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ thread_id: 'thread-A' })
    );
  });

  test('/history prints fetched messages', async () => {
    fetchMessagesMock.mockResolvedValueOnce([
      {
        id: 'm1',
        role: 'user',
        content: 'first ask',
        created_at: new Date().toISOString(),
        tool_name: null,
        tool_call_id: null,
        citations: null,
        tool_executions: null,
      },
      {
        id: 'm2',
        role: 'assistant',
        content: 'first reply',
        created_at: new Date().toISOString(),
        tool_name: null,
        tool_call_id: null,
        citations: null,
        tool_executions: null,
      },
    ]);
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/history 5' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);

    await runRepl();

    expect(fetchMessagesMock).toHaveBeenCalledWith('stale-thread');
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => m.includes('you: first ask'))).toBe(true);
    expect(messages.some((m) => m.includes('agent: first reply'))).toBe(true);
  });

  test('/forget removes from registry and clears the active thread', async () => {
    const store = await import('../services/threadStore');
    store.upsertThread({ id: 'stale-thread', title: 'To remove' });

    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/forget' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);

    await runRepl();

    expect(store.getThread('stale-thread')).toBeNull();
    expect(mockedSaveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ thread_id: null })
    );
  });
});

describe('streamToTerminal: tool args, citations footer, usage', () => {
  test('passes tool args into the spinner label', async () => {
    const startSpy = vi.fn();
    const stopSpy = vi.fn();
    const spinnerFactory = prompts.spinner as unknown as Mock;
    spinnerFactory.mockImplementation(() => ({
      start: startSpy,
      stop: stopSpy,
      error: vi.fn(),
      cancel: vi.fn(),
    }));

    mockedStreamAgent.mockReturnValueOnce(
      events([
        { type: 'tool_start', tool: 'search_documents', args: 'query=foo' },
        {
          type: 'tool_end',
          tool: 'search_documents',
          isError: false,
          result: '',
        },
        { type: 'done' },
      ])
    );

    await runRepl();

    expect(startSpy).toHaveBeenCalledWith(
      expect.stringContaining('search_documents')
    );
    expect(startSpy).toHaveBeenCalledWith(expect.stringContaining('query=foo'));
  });

  test('renders a citations footer on done when rag_context arrived', async () => {
    mockedStreamAgent.mockReturnValueOnce(
      events([
        {
          type: 'rag_context',
          contexts: [
            {
              document_id: 'doc-aaaaaaaa-1',
              title: 'Paper One',
              score: 0.91,
            },
            {
              document_id: 'doc-bbbbbbbb-2',
              title: 'Paper Two',
              score: 0.42,
            },
            // duplicate of the first — should dedupe
            {
              document_id: 'doc-aaaaaaaa-1',
              title: 'Paper One',
              score: 0.91,
            },
          ],
        },
        { type: 'token', content: 'reply' },
        { type: 'done' },
      ])
    );

    await runRepl();

    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => /— Citations —/.test(m))).toBe(true);
    expect(messages.some((m) => /\[1\] Paper One/.test(m))).toBe(true);
    expect(messages.some((m) => /\[2\] Paper Two/.test(m))).toBe(true);
    // The duplicate (doc-aaaaaaaa-1) must not yield a [3] line
    expect(messages.some((m) => /\[3\]/.test(m))).toBe(false);
  });

  test('prints a usage line when a usage event is received', async () => {
    mockedStreamAgent.mockReturnValueOnce(
      events([
        { type: 'token', content: 'hi' },
        {
          type: 'usage',
          inputTokens: 312,
          outputTokens: 540,
          costUsd: 0.0042,
        },
        { type: 'done' },
      ])
    );

    await runRepl();

    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(
      messages.some((m) => /→ 312 in \/ 540 out · \$0\.0042/.test(m))
    ).toBe(true);
  });

  test('a revising reflection restarts the response block (no concatenation)', async () => {
    const writeSpy = vi
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true);

    mockedStreamAgent.mockReturnValueOnce(
      events([
        { type: 'token', content: 'flawed draft' },
        {
          type: 'reflection',
          passed: false,
          issues: ['missing citations'],
          round: 0,
          revising: true,
        },
        { type: 'token', content: 'corrected answer' },
        { type: 'done' },
      ])
    );

    await runRepl();

    const out = writeSpy.mock.calls.map((c) => String(c[0])).join('');
    // Two NOUS block headers = draft block was closed and a fresh block began.
    // Before the fix the writer was never reset → one block, both texts glued.
    expect((out.match(/NOUS/g) || []).length).toBe(2);
    expect(out).toContain('flawed draft');
    expect(out).toContain('corrected answer');
  });
});

describe('renderCitationsFooter / renderUsageLine (pure helpers)', () => {
  test('renderCitationsFooter is a no-op when no contexts', () => {
    renderCitationsFooter([]);
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => /Citations/.test(m))).toBe(false);
  });

  test('renderUsageLine skips when both counts are zero', () => {
    renderUsageLine({ inputTokens: 0, outputTokens: 0, costUsd: null });
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => /in \/ /.test(m))).toBe(false);
  });

  test('renderUsageLine omits cost when null or zero', () => {
    renderUsageLine({ inputTokens: 5, outputTokens: 10, costUsd: null });
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => /→ 5 in \/ 10 out$/.test(m))).toBe(true);
  });
});

describe('slash commands: /projects', () => {
  const fetchProjectsMock = projectsService.fetchProjects as unknown as Mock;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mockedSelect = (prompts as any).select as Mock;

  beforeEach(() => {
    fetchProjectsMock.mockReset();
    mockedSelect.mockReset();
  });

  test('/projects sets the active project from the picker', async () => {
    fetchProjectsMock.mockResolvedValueOnce([
      {
        id: 'proj_1',
        name: 'Alpha',
        description: null,
        project_type: 'research',
        research_status: 'active',
        document_count: 3,
        note_count: 1,
        draft_count: 0,
        updated_at: '2026-04-25T00:00:00Z',
      },
      {
        id: 'proj_2',
        name: 'Beta',
        description: null,
        project_type: 'research',
        research_status: 'active',
        document_count: 0,
        note_count: 0,
        draft_count: 0,
        updated_at: '2026-04-20T00:00:00Z',
      },
    ]);
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/projects' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    mockedSelect.mockResolvedValueOnce('proj_2' as never);

    await runRepl();

    expect(fetchProjectsMock).toHaveBeenCalled();
    // After picking, future streams must include project context — assert that
    // the success log was printed with the project name.
    const successCalls = mockedLog.success.mock.calls.map(
      (c) => c[0] as string
    );
    expect(successCalls.some((m) => /Project set: Beta/.test(m))).toBe(true);
  });

  test('/projects can clear the project context', async () => {
    fetchProjectsMock.mockResolvedValueOnce([
      {
        id: 'proj_1',
        name: 'Alpha',
        description: null,
        project_type: 'research',
        research_status: 'active',
        document_count: 0,
        note_count: 0,
        draft_count: 0,
        updated_at: '2026-04-25T00:00:00Z',
      },
    ]);
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/projects' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    mockedSelect.mockResolvedValueOnce('__clear__' as never);

    await runRepl();

    const successCalls = mockedLog.success.mock.calls.map(
      (c) => c[0] as string
    );
    expect(successCalls.some((m) => /Project context cleared/.test(m))).toBe(
      true
    );
  });
});

describe('streamToTerminal: withRetry on initial call', () => {
  test('retries once on network error then succeeds', async () => {
    const netErr = Object.assign(new Error('fetch failed'), {
      code: 'ECONNREFUSED',
    });
    mockedStreamAgent.mockImplementationOnce(() => {
      throw netErr;
    });
    mockedStreamAgent.mockReturnValueOnce(
      events([{ type: 'token', content: 'hi' }, { type: 'done' }])
    );
    await runRepl();
    expect(mockedStreamAgent).toHaveBeenCalledTimes(2);
  });

  test('does NOT retry on auth error', async () => {
    mockedStreamAgent.mockImplementationOnce(() => {
      throw new Error('Stream failed: 401');
    });
    await runRepl();
    expect(mockedStreamAgent).toHaveBeenCalledTimes(1);
    expect(mockedLog.error).toHaveBeenCalledWith(
      expect.stringMatching(/expired or unauthorized/i)
    );
  });
});

describe('SIGINT scoping', () => {
  test('SIGINT mid-stream aborts the stream and returns to prompt (does not exit)', async () => {
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation((() => undefined) as never);

    let abortRef: AbortSignal | undefined;
    mockedStreamAgent.mockImplementationOnce((_msg, _ctx, opts) => {
      abortRef = opts?.signal;
      return (async function* () {
        // wait until aborted
        await new Promise<void>((resolve) => {
          abortRef?.addEventListener('abort', () => resolve(), { once: true });
        });
        const e = new Error('aborted');
        e.name = 'AbortError';
        throw e;
      })();
    });
    mockedStreamAgent.mockReturnValueOnce(
      events([{ type: 'token', content: 'second' }, { type: 'done' }])
    );

    // After first prompt, send SIGINT during stream
    setTimeout(() => process.emit('SIGINT' as never), 20);

    await runRepl();

    expect(exitSpy).not.toHaveBeenCalled();
    expect(abortRef?.aborted).toBe(true);
    exitSpy.mockRestore();
  });
});

describe('confirmKey', () => {
  test('non-TTY path delegates to p.confirm and existing mocks still work', async () => {
    // Existing tests already mock p.confirm to return true. Just assert
    // the confirmation flow still completes without raw-mode interaction.
    mockedStreamAgent.mockReturnValueOnce(
      events([
        {
          type: 'confirmation',
          threadId: 'thread-x',
          details: {
            approval_id: 'a'.repeat(64),
            tools: [{ name: 'create_project', args: {} }],
          },
        },
      ])
    );
    mockedStreamConfirm.mockReturnValueOnce(events([{ type: 'done' }]));
    await runRepl();
    expect(mockedConfirm).toHaveBeenCalled();
  });
});

describe('/retry', () => {
  test('re-runs the last user prompt against streamAgent', async () => {
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('hi' as never)
      .mockResolvedValueOnce('/retry' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    mockedStreamAgent.mockReturnValueOnce(
      events([{ type: 'token', content: 'first' }, { type: 'done' }])
    );
    mockedStreamAgent.mockReturnValueOnce(
      events([{ type: 'token', content: 'second' }, { type: 'done' }])
    );
    await runRepl();
    expect(mockedStreamAgent).toHaveBeenCalledTimes(2);
    expect(mockedStreamAgent).toHaveBeenNthCalledWith(
      1,
      'hi',
      expect.any(Object),
      expect.any(Object)
    );
    expect(mockedStreamAgent).toHaveBeenNthCalledWith(
      2,
      'hi',
      expect.any(Object),
      expect.any(Object)
    );
  });

  test('/retry with no prior message logs a hint and does not call streamAgent', async () => {
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/retry' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    await runRepl();
    expect(mockedStreamAgent).not.toHaveBeenCalled();
    const warnings = mockedLog.warn.mock.calls.map((c) => c[0] as string);
    expect(warnings.some((m) => /Nothing to retry/i.test(m))).toBe(true);
  });
});

describe('draft restore on startup', () => {
  test('first readPrompt receives initialValue from ~/.nous/draft.txt; file is then empty', async () => {
    const draft = await import('../services/draft');
    draft.writeDraft('half typed');
    mockedText.mockReset();
    mockedText.mockResolvedValueOnce('__CANCEL__' as never);
    await runRepl();
    expect(
      (mockedText.mock.calls[0]?.[0] as { initialValue?: string }).initialValue
    ).toBe('half typed');
    expect(draft.readDraft()).toBe('');
  });
});

describe('slash commands: /clear and unknown', () => {
  test('/clear is recognized and does NOT forward to streamAgent', async () => {
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/clear' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);

    await runRepl();

    expect(mockedStreamAgent).not.toHaveBeenCalled();
    // non-TTY path prints a separator instead of escape sequences
    const messages = mockedLog.message.mock.calls.map((c) => c[0] as string);
    expect(messages.some((m) => /cleared/i.test(m))).toBe(true);
  });

  test('unknown slash command warns the user and does NOT forward to streamAgent', async () => {
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('/totallymadeup' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);

    await runRepl();

    expect(mockedStreamAgent).not.toHaveBeenCalled();
    const warnings = mockedLog.warn.mock.calls.map((c) => c[0] as string);
    expect(
      warnings.some((m) => /Unknown command: \/totallymadeup/.test(m))
    ).toBe(true);
  });

  test('plain prose still reaches streamAgent (regression guard)', async () => {
    mockedText.mockReset();
    mockedText
      .mockResolvedValueOnce('hello there' as never)
      .mockResolvedValueOnce('__CANCEL__' as never);
    mockedStreamAgent.mockReturnValueOnce(
      events([{ type: 'token', content: 'hi' }, { type: 'done' }])
    );

    await runRepl();

    expect(mockedStreamAgent).toHaveBeenCalledTimes(1);
    expect(mockedStreamAgent).toHaveBeenCalledWith(
      'hello there',
      expect.any(Object),
      expect.any(Object)
    );
  });
});

describe('formatToolEndLine — pure rendering', () => {
  test('JSON error result → tool ✗ message', () => {
    const out = formatToolEndLine(
      'ingest_arxiv_papers',
      JSON.stringify({ error: 'Invalid arXiv ID format' }),
      false
    );
    expect(out).toBe('ingest_arxiv_papers Invalid arXiv ID format');
  });

  test('isError=true with non-JSON result falls back to truncated raw', () => {
    const out = formatToolEndLine('foo', 'something exploded', true);
    expect(out).toBe('foo something exploded');
  });

  test('dict-success with documents_ingested + paper_ids → N of M ingested', () => {
    const out = formatToolEndLine(
      'ingest_arxiv_papers',
      JSON.stringify({
        documents_ingested: 0,
        paper_ids: ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'],
      }),
      false
    );
    expect(out).toBe('ingest_arxiv_papers · 0 of 10 ingested');
  });

  test('dict-success with total + projects → 7 projects', () => {
    const out = formatToolEndLine(
      'list_projects',
      JSON.stringify({ total: 7, projects: [{}, {}, {}, {}, {}, {}, {}] }),
      false
    );
    expect(out).toBe('list_projects · 7 projects');
  });

  test('dict-success with project_id and name → created "<name>"', () => {
    const out = formatToolEndLine(
      'create_project',
      JSON.stringify({ project_id: 'abc12345-...', name: 'RAG Research' }),
      false
    );
    expect(out).toBe('create_project · created RAG Research');
  });

  test('dict-success with no recognized keys → bare tool', () => {
    const out = formatToolEndLine(
      'add_document_to_project',
      JSON.stringify({ ok: true }),
      false
    );
    expect(out).toBe('add_document_to_project');
  });

  test('empty result → bare tool', () => {
    expect(formatToolEndLine('search', '', false)).toBe('search');
    expect(formatToolEndLine('search', '{}', false)).toBe('search');
    expect(formatToolEndLine('search', '[]', false)).toBe('search');
  });

  test('explicit summary key beats heuristics', () => {
    const out = formatToolEndLine(
      'whatever',
      JSON.stringify({ summary: 'all good', total: 999, papers: [] }),
      false
    );
    expect(out).toBe('whatever · all good');
  });

  test('non-JSON string result → first 80 chars after ·', () => {
    const out = formatToolEndLine('thing', 'plain text result', false);
    expect(out).toBe('thing · plain text result');
  });

  test('long string result is truncated to 80 chars with ellipsis', () => {
    const long = 'a'.repeat(120);
    const out = formatToolEndLine('thing', long, false);
    // 'thing · ' + truncated to 80 visible chars
    expect(out.length).toBeLessThanOrEqual('thing · '.length + 80);
    expect(out.endsWith('…')).toBe(true);
  });
});

describe('pickSummary — heuristic priority', () => {
  test('summary key wins over everything', () => {
    expect(
      pickSummary({ summary: 'X', documents_ingested: 5, paper_ids: [1, 2] })
    ).toBe('X');
  });
  test('returns null when no recognized keys', () => {
    expect(pickSummary({ random: true })).toBeNull();
  });
  test('uppercase Name wins over id slice', () => {
    expect(pickSummary({ project_id: 'abc12345-...', name: 'Hello' })).toBe(
      'created Hello'
    );
  });
  test('falls back to id slice when name is empty', () => {
    expect(pickSummary({ project_id: 'abc12345-tail', name: '' })).toBe(
      'created abc12345'
    );
  });
});
