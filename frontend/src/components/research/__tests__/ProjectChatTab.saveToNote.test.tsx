/**
 * ProjectChatTab — save-to-note and unlink dialogs must not report success
 * when the store swallowed a request failure.
 *
 * `projectChatStore.saveThreadToNote` / `unlinkThreadFromProject` catch every
 * error, write it to `errors[projectId]` and resolve (null / void) instead of
 * throwing. The tab's handlers are the only place that can turn that into
 * user-visible feedback, so these tests drive the REAL hook + store with a
 * mocked service and assert on what the user sees.
 *
 * Mutation check (docs/engineering/testing.md): the guards live in
 * `ProjectChatTab.tsx` — `handleSaveToNote` (result check + throw) and
 * `confirmUnlink` (store-error check + inline error). Revert either and the
 * matching "fails" test below goes red:
 *   pnpm --dir frontend exec vitest run \
 *     src/components/research/__tests__/ProjectChatTab.saveToNote.test.tsx
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock('@/services/projectChatService', () => ({
  projectChatService: {
    listProjectThreads: vi.fn(),
    startChatFromProject: vi.fn(),
    linkThreadToProject: vi.fn(),
    unlinkThreadFromProject: vi.fn(),
    saveThreadToNote: vi.fn(),
  },
}));

vi.mock('@/services/projectService', () => ({
  projectService: {
    getProject: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listConversations: vi.fn().mockResolvedValue({ conversations: [] }),
    listThreads: vi.fn().mockResolvedValue({ threads: [] }),
  },
}));

const setThreadProjectBinding = vi.fn();
vi.mock('@/store/chat-store', () => ({
  useChatStore: {
    getState: () => ({ setThreadProjectBinding }),
  },
}));

import { projectChatService } from '@/services/projectChatService';
import { projectService } from '@/services/projectService';
import { useProjectChatStore } from '@/store/projectChatStore';
import type { ProjectThread } from '@/types/project-chat';
import { ProjectChatTab } from '../ProjectChatTab';

const PROJECT_ID = 'proj-1';
const THREAD_ID = 'thread-1';

const thread: ProjectThread = {
  id: 'pt-1',
  project_id: PROJECT_ID,
  thread_id: THREAD_ID,
  thread_title: 'Quarterly review discussion',
  conversation_id: 'conv-1',
  link_type: 'manual',
  linked_at: '2026-01-01T00:00:00Z',
  linked_by_id: 'user-1',
  context_note: null,
  message_count: 3,
  last_message_at: '2026-01-01T00:00:00Z',
};

const service = vi.mocked(projectChatService);

function resetStoreWithOneThread(): void {
  vi.clearAllMocks();
  act(() => {
    useProjectChatStore.getState().reset();
  });
  vi.mocked(projectService.getProject).mockResolvedValue({
    workspace_id: 'ws-1',
  } as Awaited<ReturnType<typeof projectService.getProject>>);
  service.listProjectThreads.mockResolvedValue({
    threads: [thread],
    total: 1,
  } as Awaited<ReturnType<typeof projectChatService.listProjectThreads>>);
}

async function renderTabWithThread(): Promise<void> {
  render(<ProjectChatTab projectId={PROJECT_ID} />);
  await screen.findByText('Quarterly review discussion');
}

async function openSaveToNoteAndSubmit(title: string): Promise<void> {
  fireEvent.click(screen.getByRole('button', { name: /save to note/i }));
  const input = await screen.findByLabelText(/note title/i);
  fireEvent.change(input, { target: { value: title } });
  // Plain click (no `act` wrapper) so the store's error write and the
  // handler's follow-up are scheduled exactly as they are in production.
  fireEvent.click(screen.getByRole('button', { name: /save note/i }));
}

async function confirmUnlinkTwice(): Promise<void> {
  // ThreadCard owns the first confirm; ProjectChatTab owns the second one
  // whose action actually calls the store.
  fireEvent.click(screen.getByRole('button', { name: 'Unlink thread' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Unlink' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Unlink' }));
}

describe('ProjectChatTab — save-to-note outcome', () => {
  beforeEach(resetStoreWithOneThread);

  it('keeps the modal open, shows the error and preserves the title when the save fails', async () => {
    service.saveThreadToNote.mockRejectedValue(new Error('Note quota exceeded'));
    await renderTabWithThread();

    await openSaveToNoteAndSubmit('My saved thread');

    // The failure must be visible inside the still-open modal …
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Note quota exceeded'
    );
    expect(screen.getByText('Save thread to note')).toBeInTheDocument();
    expect(screen.getByLabelText(/note title/i)).toHaveValue('My saved thread');
    // … and must not hijack the whole tab as a load failure.
    expect(screen.queryByText('Failed to load threads')).toBeNull();
    expect(useProjectChatStore.getState().errors[PROJECT_ID]).toBeNull();
  });

  it('closes the modal when the save succeeds', async () => {
    service.saveThreadToNote.mockResolvedValue({
      id: 'note-1',
    } as Awaited<ReturnType<typeof projectChatService.saveThreadToNote>>);
    await renderTabWithThread();

    await openSaveToNoteAndSubmit('My saved thread');

    await waitFor(() =>
      expect(screen.queryByText('Save thread to note')).toBeNull()
    );
    expect(service.saveThreadToNote).toHaveBeenCalledWith(PROJECT_ID, {
      thread_id: THREAD_ID,
      note_title: 'My saved thread',
      include_citations: true,
    });
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('ProjectChatTab — unlink outcome', () => {
  beforeEach(resetStoreWithOneThread);

  it('keeps the confirm dialog open and surfaces the error when the unlink fails', async () => {
    service.unlinkThreadFromProject.mockRejectedValue(
      new Error('Thread is locked by another session')
    );
    await renderTabWithThread();

    await confirmUnlinkTwice();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Thread is locked by another session'
    );
    expect(
      screen.getByText('Unlink thread from project?')
    ).toBeInTheDocument();
    // The thread is still linked and the tab must not flip to the load-failure screen.
    expect(screen.getByText('Quarterly review discussion')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load threads')).toBeNull();
    expect(useProjectChatStore.getState().errors[PROJECT_ID]).toBeNull();
  });

  it('closes the confirm dialog and removes the thread when the unlink succeeds', async () => {
    service.unlinkThreadFromProject.mockResolvedValue(undefined);
    await renderTabWithThread();

    await confirmUnlinkTwice();

    await waitFor(() =>
      expect(screen.queryByText('Unlink thread from project?')).toBeNull()
    );
    expect(service.unlinkThreadFromProject).toHaveBeenCalledWith(
      PROJECT_ID,
      THREAD_ID
    );
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Quarterly review discussion')).toBeNull();
  });
});
