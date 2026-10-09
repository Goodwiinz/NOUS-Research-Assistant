import {
  beginArtifactNavigation,
  confirmArtifactNavigation,
} from '@/utils/artifactNavigation';
import { useChatSessionGuard } from './useChatSessionGuard';
import { onChatSessionReset } from '@/store/chat-store';
import { useCallback, useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';

import {
  getNewChatUrl,
  getSelectedThreadUrl,
} from '@/components/chat/shared/chatNavigation';
import type {
  CommandAction,
  CommandOutput,
  CommandOutputItem,
} from '@/components/chat/commandOutput';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import {
  SLASH_COMMANDS,
  type SlashCommandId,
} from '@/components/chat/slashCommands';
import { ChatConversation } from '@/hooks/chat/chatTypes';
import { documentService } from '@/services/documentService';
import { projectService } from '@/services/projectService';
import { useProjectChatStore } from '@/store/projectChatStore';
import { useProjectStore } from '@/store/projectStore';

function relativeTime(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 60_000) return 'just now';
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(ms).toLocaleDateString();
}

// ============================================
// HOOK PARAMS
// ============================================

export interface UseSlashCommandsParams {
  input: string;
  setInput: React.Dispatch<React.SetStateAction<string>>;
  handleSubmit: (
    contentOverride?: string,
    historyOverride?: ChatPageMessage[]
  ) => Promise<void>;
  /** Regenerate-most-recent-assistant-turn (useChatComposerActions) — the
   * /retry command dispatches to it rather than re-implementing regenerate. */
  retryLast: () => void;
  conversations: ChatConversation[];
  activeThreadId: string | null;
  setCurrentThread: (threadId: string | null) => void;
  chatInputRef: React.RefObject<HTMLTextAreaElement>;
}

export interface UseSlashCommandsReturn {
  commandOutputs: CommandOutput[];
  handleSlashCommand: (id: SlashCommandId) => void;
  handleCommandItemAction: (action: CommandAction) => void;
  /** True when assistant-ui should send the draft; false when a local command
   * consumed it. */
  submitMessage: (attachmentIds?: string[]) => boolean;
  startNewChat: () => void;
}

// ============================================
// HOOK
// ============================================

/**
 * Slash-command execution (CLI-style, output printed into the transcript),
 * the /remember|/memories send-time interception, and the small set of
 * composer actions the command vocabulary itself triggers (/new, /retry via
 * useChatComposerActions.retryLast) or that a command result routes to
 * (/threads, /projects project-context binding). Output is ephemeral page
 * state — never sent to the agent or persisted — cleared on thread change
 * and on real message send.
 */
export function useSlashCommands({
  input,
  setInput,
  handleSubmit,
  retryLast,
  conversations,
  activeThreadId,
  setCurrentThread,
  chatInputRef,
}: UseSlashCommandsParams): UseSlashCommandsReturn {
  const router = useRouter();
  const isCurrentSession = useChatSessionGuard();
  const [commandOutputs, setCommandOutputs] = useState<CommandOutput[]>([]);
  useEffect(() => onChatSessionReset(() => setCommandOutputs([])), []);
  const fetchProjects = useProjectStore((s) => s.fetchProjects);

  // Clear ephemeral command output on thread change. Reset during render
  // (React's documented "adjusting state when a prop changes" pattern)
  // rather than in an effect, which would call setState synchronously and
  // trigger a redundant extra render.
  const [prevThreadIdForCommands, setPrevThreadIdForCommands] =
    useState(activeThreadId);
  if (activeThreadId !== prevThreadIdForCommands) {
    setPrevThreadIdForCommands(activeThreadId);
    setCommandOutputs([]);
  }

  const appendOutput = useCallback(
    (o: CommandOutput) => {
      if (isCurrentSession()) setCommandOutputs((prev) => [...prev, o]);
    },
    [isCurrentSession]
  );

  const patchOutput = useCallback(
    (id: string, patch: Partial<CommandOutput>) => {
      if (!isCurrentSession()) return;
      setCommandOutputs((prev) =>
        prev.map((o) => (o.id === id ? { ...o, ...patch } : o))
      );
    },
    [isCurrentSession]
  );

  // Start a fresh chat — shared by the sidebar "new" button and the /new command
  const startNewChat = useCallback(() => {
    if (!confirmArtifactNavigation()) return;
    setCommandOutputs([]);
    setCurrentThread(null);
    router.push(getNewChatUrl());
  }, [router, setCurrentThread]);

  // Bind the chat to a project. The ?projectId= param alone is only the
  // pre-thread intent: resolveBoundProjectId ignores it as soon as the thread
  // row is in the store, and thread navigation drops it. So persist the
  // binding on the thread itself (server row + store mirror), exactly like
  // the context rail's picker, and keep the param for the not-yet-created
  // thread of a brand-new chat.
  const handleSetProjectContext = useCallback(
    (projectId: string, projectName: string) => {
      if (!isCurrentSession()) return;
      const finishNavigation = beginArtifactNavigation();
      if (!finishNavigation) return;
      const params = new URLSearchParams(window.location.search);
      params.set('projectId', projectId);
      router.replace(`/chat?${params.toString()}`);

      const outId = `cmd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setCommandOutputs([
        {
          id: outId,
          command: '/projects',
          timestamp: Date.now(),
          status: 'ready',
          lines: [`Project context set: ${projectName}`],
        },
      ]);

      if (!activeThreadId) {
        finishNavigation();
        return;
      }
      void (async () => {
        try {
          const linked = await useProjectChatStore
            .getState()
            .linkThreadToProject(projectId, { thread_id: activeThreadId });
          if (!isCurrentSession() || linked) return;
          // Drop the param we optimistically set: a thread the store has not
          // loaded yet DOES read it, so leaving it behind would hand the agent
          // a project the attach just failed to make real.
          const current = new URLSearchParams(window.location.search);
          if (current.get('projectId') === projectId) {
            current.delete('projectId');
            const query = current.toString();
            router.replace(query ? `/chat?${query}` : '/chat');
          }
          // The param can't stand in for a thread that is already loaded, so a
          // failed attach means no project context at all — say so instead of
          // leaving the success line up.
          patchOutput(outId, {
            lines: [
              `Could not attach this chat to ${projectName}.`,
              useProjectChatStore.getState().errors[projectId] ||
                'Please try again.',
            ],
          });
        } finally {
          finishNavigation();
        }
      })();
    },
    [router, activeThreadId, patchOutput, isCurrentSession]
  );

  // Project-scoped memory commands. `/remember <fact>` saves a durable fact the
  // agent recalls across every thread in the project; `/memories` lists them
  // (tap a row to delete). The active project is the one the chat is bound to
  // (?projectId=), falling back to the selected project in the store.
  const runMemoryCommand = useCallback(
    (kind: 'remember' | 'memories', content: string) => {
      if (!isCurrentSession()) return;
      const now = Date.now();
      const outId = `cmd-${now}-${Math.random().toString(36).slice(2, 8)}`;
      const projectId =
        new URLSearchParams(window.location.search).get('projectId') ||
        useProjectStore.getState().currentProject?.id ||
        null;

      if (!projectId) {
        appendOutput({
          id: outId,
          command: `/${kind}`,
          timestamp: now,
          status: 'ready',
          lines: ['No project in context.'],
          note: 'Set one with /projects, then try again.',
        });
        return;
      }

      if (kind === 'remember') {
        const fact = content.trim();
        if (!fact) {
          appendOutput({
            id: outId,
            command: '/remember',
            timestamp: now,
            status: 'ready',
            lines: ['Usage: /remember <fact>'],
            note: 'e.g. /remember Always cite sources in APA style.',
          });
          return;
        }
        appendOutput({
          id: outId,
          command: '/remember',
          timestamp: now,
          status: 'loading',
          lines: [],
        });
        void (async () => {
          try {
            await projectService.createMemory(projectId, fact, 'remember');
            patchOutput(outId, {
              status: 'ready',
              lines: [`Saved to project memory: ${fact}`],
              note: 'NOUS recalls this across every thread in this project.',
            });
          } catch {
            patchOutput(outId, {
              status: 'ready',
              lines: ['Could not save memory.'],
            });
          }
        })();
        return;
      }

      // kind === 'memories'
      appendOutput({
        id: outId,
        command: '/memories',
        timestamp: now,
        status: 'loading',
        items: [],
      });
      void (async () => {
        try {
          const res = await projectService.listMemories(projectId);
          const items: CommandOutputItem[] = (res.memories ?? []).map((m) => ({
            key: m.id,
            label: m.content,
            meta: m.source === 'remember' ? 'remembered' : m.source,
            action: { type: 'delete-memory', id: m.id, projectId },
          }));
          patchOutput(outId, {
            status: 'ready',
            items,
            emptyText: 'No project memory yet.',
            note: items.length ? 'Tap a memory to delete it.' : undefined,
          });
        } catch {
          patchOutput(outId, {
            status: 'ready',
            items: [],
            emptyText: 'Could not load memory.',
          });
        }
      })();
    },
    [appendOutput, patchOutput, isCurrentSession]
  );

  // Run a slash command. Output prints into the chat transcript, CLI-style;
  // nothing navigates away (except /new, which starts a fresh chat).
  const handleSlashCommand = useCallback(
    (id: SlashCommandId) => {
      if (!isCurrentSession()) return;
      const now = Date.now();
      const outId = `cmd-${now}-${Math.random().toString(36).slice(2, 8)}`;
      switch (id) {
        case 'new':
          startNewChat();
          return;
        case 'retry':
          retryLast();
          return;
        case 'clear':
          setCommandOutputs([]);
          setInput('');
          return;
        case 'summarize':
        case 'keypoints':
        case 'gaps':
        case 'timeline': {
          // Synthesis commands SEND a real, citation-asking research query over
          // the current project sources (unlike /threads, which only prints
          // in-chat output). Mirror submitMessage by clearing ephemeral command
          // output first, then dispatch the templated prompt through the same
          // send path useChatComposerActions.retryLast/handleRegenerate use:
          // setInput so the composer reflects what was sent, then a deferred
          // handleSubmit(template) so it doesn't read the stale pre-setInput
          // value from its closure.
          const SYNTHESIS_TEMPLATES: Record<
            'summarize' | 'keypoints' | 'gaps' | 'timeline',
            string
          > = {
            summarize:
              'Summarize the key findings across my sources, with citations.',
            keypoints:
              'List the key points from my sources as concise bullets, each with a citation.',
            gaps: 'What gaps, open questions, or contradictions appear across my sources? Cite them.',
            timeline:
              'Build a chronological timeline of the developments described in my sources, with citations.',
          };
          const template = SYNTHESIS_TEMPLATES[id];
          setCommandOutputs([]);
          setInput(template);
          setTimeout(() => handleSubmit(template), 0);
          return;
        }
        case 'help':
          appendOutput({
            id: outId,
            command: '/help',
            timestamp: now,
            status: 'ready',
            lines: SLASH_COMMANDS.map((c) => `${c.label.padEnd(10)}${c.title}`),
            note: 'Type / in the message box to autocomplete.',
          });
          return;
        case 'threads': {
          const items: CommandOutputItem[] = conversations.map((c) => ({
            key: c.id,
            label: c.title || 'Untitled',
            meta: c.updatedAt ? relativeTime(c.updatedAt) : undefined,
            active: c.id === activeThreadId,
            action: { type: 'open-thread', id: c.id },
          }));
          appendOutput({
            id: outId,
            command: '/threads',
            timestamp: now,
            status: 'ready',
            items,
            emptyText: 'No threads yet.',
            note: items.length ? 'Tap a thread to open it.' : undefined,
          });
          return;
        }
        case 'projects':
          appendOutput({
            id: outId,
            command: '/projects',
            timestamp: now,
            status: 'loading',
            items: [],
          });
          void (async () => {
            try {
              await fetchProjects({ limit: 20, project_status: 'active' });
              const { projects, currentProject } = useProjectStore.getState();
              const contextProjectId =
                new URLSearchParams(window.location.search).get('projectId') ||
                currentProject?.id ||
                null;
              const items: CommandOutputItem[] = projects.map((p) => ({
                key: p.id,
                label: p.name,
                meta: `${p.document_count ?? 0} ${
                  (p.document_count ?? 0) === 1 ? 'paper' : 'papers'
                }`,
                active: p.id === contextProjectId,
                action: { type: 'set-project', id: p.id, name: p.name },
              }));
              patchOutput(outId, {
                status: 'ready',
                items,
                emptyText: 'No active projects.',
                note: items.length
                  ? 'Tap a project to use it as context.'
                  : undefined,
              });
            } catch {
              patchOutput(outId, {
                status: 'ready',
                items: [],
                emptyText: 'Could not load projects.',
              });
            }
          })();
          return;
        case 'papers':
          appendOutput({
            id: outId,
            command: '/papers',
            timestamp: now,
            status: 'loading',
            items: [],
          });
          void (async () => {
            try {
              const res = await documentService.getDocuments(1, 10);
              const docs = res?.documents ?? [];
              const items: CommandOutputItem[] = docs.map((d) => ({
                key: d.id,
                label: d.title || d.filename,
                meta: d.processing_status,
                action: {
                  type: 'cite-paper',
                  id: d.id,
                  title: d.title || d.filename,
                },
              }));
              patchOutput(outId, {
                status: 'ready',
                items,
                emptyText: 'No papers found.',
                note: items.length
                  ? 'Tap a paper to reference it in your message.'
                  : undefined,
              });
            } catch {
              patchOutput(outId, {
                status: 'ready',
                items: [],
                emptyText: 'Could not load papers.',
              });
            }
          })();
          return;
        case 'remember':
          runMemoryCommand('remember', '');
          return;
        case 'memories':
          runMemoryCommand('memories', '');
          return;
      }
    },
    [
      startNewChat,
      retryLast,
      setInput,
      handleSubmit,
      conversations,
      activeThreadId,
      appendOutput,
      patchOutput,
      fetchProjects,
      runMemoryCommand,
      isCurrentSession,
    ]
  );

  // A tap on a clickable command-output row.
  const handleCommandItemAction = useCallback(
    (action: CommandAction) => {
      if (!isCurrentSession()) return;
      switch (action.type) {
        case 'open-thread':
          if (!confirmArtifactNavigation()) return;
          setCurrentThread(action.id);
          router.push(getSelectedThreadUrl(action.id));
          return;
        case 'set-project':
          handleSetProjectContext(action.id, action.name);
          return;
        case 'cite-paper':
          setInput((cur) =>
            cur ? `${cur} "${action.title}"` : `"${action.title}" `
          );
          chatInputRef.current?.focus();
          return;
        case 'delete-memory':
          void (async () => {
            try {
              await projectService.deleteMemory(action.projectId, action.id);
            } catch {
              /* best-effort; re-list reflects the true state either way */
            }
            runMemoryCommand('memories', '');
          })();
          return;
      }
    },
    [
      router,
      setCurrentThread,
      handleSetProjectContext,
      setInput,
      chatInputRef,
      runMemoryCommand,
      isCurrentSession,
    ]
  );

  // Clear ephemeral command output when a real message is sent. Slash commands
  // that carry an argument (`/remember <fact>`) never open the autocomplete
  // menu — they look like a normal message — so they're intercepted here on
  // send instead of being dispatched to the agent.
  const submitMessage = useCallback(
    (_attachmentIds?: string[]) => {
      const raw = input.trim();
      const memMatch = raw.match(/^\/(remember|memories)\b\s*([\s\S]*)$/i);
      if (memMatch) {
        setInput('');
        runMemoryCommand(
          memMatch[1].toLowerCase() as 'remember' | 'memories',
          memMatch[2]
        );
        return false;
      }
      setCommandOutputs([]);
      return true;
    },
    [input, setInput, runMemoryCommand]
  );

  return {
    commandOutputs,
    handleSlashCommand,
    handleCommandItemAction,
    submitMessage,
    startNewChat,
  };
}
