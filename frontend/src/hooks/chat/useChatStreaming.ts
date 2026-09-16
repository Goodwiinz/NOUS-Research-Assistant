'use client';

import type {
  ActivityStep,
  ChatPageMessage,
} from '@/components/chat/shared/cloudMessageView';
import {
  mapDbToolExecutions,
  summarizeToolArgs,
  summarizeToolResult,
} from '@/components/chat/shared/cloudMessageView';
import toast from 'react-hot-toast';

import { getSelectedThreadUrl } from '@/components/chat/shared/chatNavigation';
import { buildThreadCreateRequest } from '@/components/chat/shared/threadCreation';
import {
  ChatConversation,
  generateConversationTitle,
} from '@/hooks/chat/chatTypes';
import type { ChatAuthRecoveryRoute } from '@/hooks/chat/useChatSession';
import {
  clearArmedChatAuthRecovery,
  consumeChatAuthRecovery,
  discardChatAuthRecovery,
  markChatAuthRecoveryReady,
  stageChatAuthRecovery,
} from '@/hooks/chat/chatAuthRecovery';
import { agentChatService } from '@/services/agentChatService';
import type { AgentStreamCallbacks } from '@/services/agentChatService';
import type {
  AgentProgressStep,
  AgentStreamPhase,
} from '@/services/agentStreamEvents';
import { workspaceService } from '@/services/workspaceService';
import {
  resolveBoundProjectId,
  selectCurrentThreadProjectId,
  useChatStore,
} from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import {
  toolLabel,
  toolStatusLabel,
} from '@/components/context-rail/toolLabels';
import { deriveAgentName, deriveTask } from '@/components/context-rail';
import { Conversation as DBConversation } from '@/types/workspace';
import type {
  CitationCreate,
  DbToolExecution,
  Workspace,
} from '@/types/workspace';
import type { PlanStep } from '@/types/agent-chat';
import { normalizeCitation } from '@/utils/citationNormalizer';
import { useQueryClient } from '@tanstack/react-query';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useProjectStore } from '@/store/projectStore';
import { useAuthStore } from '@/stores/authStore';
import { v5 as uuidv5 } from 'uuid';

/**
 * Map a raw rag_context SSE item ({document_id, title, content, score} from
 * the agent's rag_node) onto the workspace CitationCreate schema so the
 * turn's sources persist with the assistant message. Snippet capped at the
 * backend Citation column limit.
 */
/**
 * Pull the created note's id/title out of a create_project_note tool result
 * (a JSON string like {status, note_id, title, …}). Null when the result
 * isn't JSON, errored, or carries no note_id — callers skip auto-focus then.
 */
export function parseCreatedNoteResult(
  result: string
): { noteId: string; projectId?: string; title?: string } | null {
  try {
    const parsed: unknown =
      typeof result === 'string' ? JSON.parse(result) : result;
    if (!parsed || typeof parsed !== 'object') return null;
    const obj = parsed as Record<string, unknown>;
    if (obj.error) return null;
    if (typeof obj.note_id !== 'string' || !obj.note_id) return null;
    return {
      noteId: obj.note_id,
      // The note's actual project — may differ from the thread's bound
      // project when the user asked for an explicit target project.
      ...(typeof obj.project_id === 'string' && obj.project_id
        ? { projectId: obj.project_id }
        : {}),
      ...(typeof obj.title === 'string' && obj.title
        ? { title: obj.title }
        : {}),
    };
  } catch {
    return null;
  }
}

/** Tool name + args preview from an interrupt's confirmation payload — flat
 * (tool_name/tool_args) or the first entry of a `tools` list. Mirrors the
 * page-level banner's extractToolCall (P4). */
function extractConfirmationPreview(
  confirmation: Record<string, unknown> | undefined
): { tools: Array<{ name: string; args: Record<string, unknown> }> } {
  if (!confirmation) return { tools: [{ name: 'this action', args: {} }] };
  const rawTools = Array.isArray(confirmation.tools) ? confirmation.tools : [];
  const tools = rawTools.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return [];
    const item = raw as Record<string, unknown>;
    if (typeof item.name !== 'string' || !item.name.trim()) return [];
    const args =
      item.args && typeof item.args === 'object' && !Array.isArray(item.args)
        ? (item.args as Record<string, unknown>)
        : {};
    return [{ name: item.name, args }];
  });
  if (tools.length > 0) return { tools };

  const flatName = confirmation.tool_name;
  const rawArgs = confirmation.tool_args ?? confirmation.args;
  if (typeof flatName === 'string' && flatName.trim()) {
    return {
      tools: [
        {
          name: flatName,
          args:
            rawArgs && typeof rawArgs === 'object' && !Array.isArray(rawArgs)
              ? (rawArgs as Record<string, unknown>)
              : {},
        },
      ],
    };
  }
  return { tools: [{ name: 'this action', args: {} }] };
}

export function toCitationCreate(ctx: Record<string, unknown>): CitationCreate {
  const documentId =
    (ctx.document_id as string | undefined) ??
    (ctx.documentId as string | undefined);
  const snippet =
    (ctx.content as string | undefined) ??
    (ctx.snippet as string | undefined) ??
    '';
  return {
    ...(documentId ? { document_id: documentId } : {}),
    ...(ctx.external_reference_id
      ? { external_reference_id: ctx.external_reference_id as string }
      : {}),
    document_title:
      (ctx.title as string | undefined) ??
      (ctx.document_title as string | undefined),
    snippet: snippet.slice(0, 2000),
    ...(typeof ctx.score === 'number' ? { score: ctx.score } : {}),
    ...(typeof (ctx.source_position ?? ctx.sourcePosition) === 'number'
      ? {
          source_position: (ctx.source_position ??
            ctx.sourcePosition) as number,
        }
      : {}),
    ...(typeof (ctx.chunk_id ?? ctx.chunkId) === 'string'
      ? { chunk_id: (ctx.chunk_id ?? ctx.chunkId) as string }
      : {}),
    ...(typeof (ctx.chunk_index ?? ctx.chunkIndex) === 'number'
      ? { chunk_index: (ctx.chunk_index ?? ctx.chunkIndex) as number }
      : {}),
    ...(typeof (ctx.page_number ?? ctx.pageNumber) === 'number'
      ? { page_number: (ctx.page_number ?? ctx.pageNumber) as number }
      : {}),
  };
}

// Agent tools that mutate project content shown in the Working folders rail
// (sources/notes/drafts). A successful run of one of these must invalidate
// the ['project', …] queries — useProjectWorkingFolders caches them for 5
// minutes, so without this the rail misses documents/notes the agent just
// created until a reload.
/** Reattach attempts per thread activation, and the waits between them. A
 * proxy-killed resume is usually transient; the run itself keeps going. */
const RESUME_MAX_ATTEMPTS = 3;
const RESUME_BACKOFF_MS = [1_000, 4_000];
const MAX_PROGRESS_STEPS = 16;
const MAX_REASONING_SUMMARY_CHARS = 8_000;

export function appendProgressStep(
  steps: AgentProgressStep[],
  phase: AgentStreamPhase,
  detail?: string
): AgentProgressStep[] {
  if (!detail) return steps;
  const next = { phase, detail: detail.slice(0, 200) };
  const last = steps[steps.length - 1];
  if (last?.phase === next.phase && last.detail === next.detail) return steps;
  return [...steps, next].slice(-MAX_PROGRESS_STEPS);
}

function resetStreamingTurnState(): void {
  useChatStore.setState({
    isStreaming: false,
    streamingContent: '',
    streamingCitations: [],
    streamingSteps: [],
    streamingPlan: [],
    streamingProgress: [],
    streamingReasoning: '',
    streamingElapsedMs: null,
    streamingPhase: null,
    streamingStatusDetail: null,
    isRetrievingRag: false,
    streamingThreadId: null,
  });
}

const PROJECT_MUTATING_TOOLS = new Set([
  'ingest_arxiv',
  'add_document_to_project',
  'create_project',
  'create_project_note',
  'create_draft',
]);

// ============================================
// TYPES
// ============================================

export interface PendingConfirmation {
  threadId: string;
  workspaceThreadId: string;
  /** Changes whenever a gate is re-armed so the approval UI resets safely. */
  approvalId: string;
  confirmation: Record<string, unknown>;
  /** Settled tool steps recorded before the interrupt, carried into the
   * resumed turn so the confirmed answer keeps its full provenance. */
  steps?: ActivityStep[];
  /** Structured plan emitted before the interrupt — carried so the
   * confirmed turn commits with its inline plan. */
  plan?: PlanStep[];
  /** Planner's top-level rationale for `plan`, carried the same way. */
  planReasoning?: string;
  progress?: AgentProgressStep[];
  /** RAG citations retrieved before the interrupt — the interrupt exit
   * clears streamingCitations, so they must ride the confirmation. */
  citations?: Array<Record<string, unknown>>;
  /** Stable identities for reconciliation across the interrupt/resume split. */
  userRuntimeId?: string;
  assistantRuntimeId?: string;
}

interface StreamAuthRecoveryAttempt {
  attemptId: string;
  ownerUserId: string | null;
  threadId: string;
  promptStaged: boolean;
  authRefreshStarted: boolean;
  navigationStarted: boolean;
}

/** Map raw planner SSE steps onto the structured inline-plan shape. */
function toTurnPlan(
  steps: Array<Record<string, unknown>> | undefined
): PlanStep[] {
  return (steps ?? [])
    .filter(
      (st): st is Record<string, unknown> => !!st && typeof st === 'object'
    )
    .map((st, i) => ({
      step: typeof st.step === 'number' ? st.step : i + 1,
      description: String(st.description ?? st.text ?? st.title ?? ''),
      tool: typeof st.tool === 'string' ? st.tool : '',
      args_hint:
        st.args_hint && typeof st.args_hint === 'object'
          ? (st.args_hint as Record<string, unknown>)
          : {},
      depends_on: Array.isArray(st.depends_on)
        ? (st.depends_on as number[])
        : [],
    }))
    .filter((p) => p.description.length > 0);
}

/** Map raw planner SSE steps onto activity-rail plan items (text + tool
 * hint, so the rail marks items done when their tool completes). Step items
 * may be plain strings or planner dicts. */
function toActivityPlanItems(
  steps: Array<Record<string, unknown> | string> | undefined
): Array<{ text: string; tool?: string }> {
  return (steps ?? [])
    .map((step) => {
      if (typeof step === 'string') return { text: step };
      if (step && typeof step === 'object') {
        const s = step as Record<string, unknown>;
        return {
          text: String(s.description ?? s.text ?? s.title ?? s.step ?? ''),
          tool: typeof s.tool === 'string' ? s.tool : undefined,
        };
      }
      return { text: '' };
    })
    .filter((item) => item.text.length > 0);
}

function toolInvocationKey(tool: string, callId?: string): string {
  return callId || tool;
}

function findRunningToolStepIndex(
  steps: ActivityStep[],
  tool: string,
  callId?: string
): number | undefined {
  if (callId) {
    const exact = steps.findIndex(
      (step) => step.id === callId && step.status === 'running'
    );
    return exact >= 0 ? exact : undefined;
  }
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (steps[index].tool === tool && steps[index].status === 'running') {
      return index;
    }
  }
  return undefined;
}

/**
 * A pending HITL confirmation may only be rendered/actioned on the thread it
 * belongs to — actioning it elsewhere injects the resumed turn's messages
 * into whatever thread happens to be displayed. workspaceThreadId is '' when
 * the turn started before any thread existed (new chat), which matches a
 * null displayed-thread id.
 */
/**
 * Replace the transcript without destroying a pending HITL approval.
 *
 * The approval card lives in the local overlay as a `pendingApproval` message
 * (see the P4 effect). Several unwind paths used to `setMessages(snapshot)`
 * with an array captured *before* the turn — silently deleting the card the
 * user has to act on, while `pendingConfirmation` stayed set and kept the
 * composer locked. The gate was then unrecoverable without a reload.
 */
export function replacePreservingApproval(
  next: ChatPageMessage[]
): (prev: ChatPageMessage[]) => ChatPageMessage[] {
  return (prev) => {
    const approvals = prev.filter((m) => m.pendingApproval);
    if (approvals.length === 0) return next;
    const carried = approvals.filter(
      (a) => !next.some((n) => n.runtimeId === a.runtimeId)
    );
    return [...next, ...carried];
  };
}

export function confirmationBelongsToThread(
  pending: PendingConfirmation | null,
  displayedThreadId: string | null
): boolean {
  if (!pending) return false;
  return pending.workspaceThreadId === (displayedThreadId ?? '');
}

export interface UseChatStreamingParams {
  messages: ChatPageMessage[];
  /** CX2: the reconciled view the user actually sees (local ∪ store) —
   * `selectDisplayedMessages` in useChatSession. handleSubmit must build the
   * posted turn from this, not `messages`: `messages` is empty during the
   * lazy-load window right after a thread switch (threads list seeds
   * `conversations` with `messages: []`), while the store already has the
   * transcript — submitting from `messages` silently dropped the history. */
  displayedMessages: ChatPageMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatPageMessage[]>>;
  conversations: ChatConversation[];
  setConversations: React.Dispatch<React.SetStateAction<ChatConversation[]>>;
  dbConversation: DBConversation | null;
  /** Active workspace scopes durable thread creation and agent authorization. */
  workspace?: Workspace | null;
  enableRAG: boolean;
  /** Embedded surfaces may own URL synchronization without navigating away.
   * The production chat defaults to the canonical `/chat` route. */
  navigateToThread?: (threadId: string) => void;
  /** Omission disables draft restoration. Route owners must explicitly prove
   * that URL intent, initialization, and selected thread have converged. */
  authRecoveryRoute?: ChatAuthRecoveryRoute;
}

export interface UseChatStreamingReturn {
  input: string;
  setInput: React.Dispatch<React.SetStateAction<string>>;
  isLoading: boolean;
  handleSubmit: (
    contentOverride?: string,
    historyOverride?: ChatPageMessage[],
    /** Edit-and-resend: the `client_message_id` of the user turn being
     * replaced. Sent as `supersedes_client_message_id` so the server
     * tombstones that turn and everything after it. */
    supersedesClientMessageId?: string,
    /** Documents the composer uploaded for this turn, sent as
     * `attachment_ids` so the server links them to the persisted user row. */
    attachmentIds?: string[]
  ) => Promise<void>;
  handleStop: () => void;
  pendingConfirmation: PendingConfirmation | null;
  isConfirming: boolean;
  handleConfirmation: (confirmed: boolean) => Promise<void>;
  chatInputRef: React.RefObject<HTMLTextAreaElement>;
  storeIsStreaming: boolean;
  storeStreamingContent: string;
  storeIsRetrievingRag: boolean;
  /** CX5: the thread id the live stream belongs to, or null when idle. Lets
   * the page gate streaming-derived rendering (typing indicator, welcome
   * state) to the thread that actually owns the stream, without touching the
   * global single-flight `storeIsStreaming` the composer blocks on. */
  streamingThreadId: string | null;
  streamingTimestampRef: React.MutableRefObject<number | null>;
}

// ============================================
// HOOK
// ============================================

export function useChatStreaming(
  params: UseChatStreamingParams
): UseChatStreamingReturn {
  const {
    messages,
    displayedMessages,
    setMessages,
    setConversations,
    dbConversation,
    workspace,
    enableRAG,
    navigateToThread,
    authRecoveryRoute,
  } = params;

  // ---- Project context (for agent page_context) ----
  const searchParams = useSearchParams();
  // Thread row first (source_project_id is the durable binding), URL param
  // only as the initial intent — thread navigation (getSelectedThreadUrl)
  // drops it. Sentinel semantics documented on selectCurrentThreadProjectId.
  const threadProjectId = useChatStore(selectCurrentThreadProjectId);
  const boundProjectId = resolveBoundProjectId(
    threadProjectId,
    searchParams.get('projectId')
  );
  const projectStoreProjects = useProjectStore((s) => s.projects);
  const currentProject = useProjectStore((s) => s.currentProject);
  const resolvedProjectName = boundProjectId
    ? currentProject?.id === boundProjectId
      ? currentProject.name
      : (projectStoreProjects.find((p) => p.id === boundProjectId)?.name ??
        undefined)
    : undefined;

  // ---- State ----
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [pendingConfirmations, setPendingConfirmations] = useState<
    Record<string, PendingConfirmation>
  >({});
  const [isConfirming, setIsConfirming] = useState(false);

  // ---- Refs ----
  const lastStreamedContentRef = useRef<string>('');
  const abortControllerRef = useRef<AbortController | null>(null);
  // Which stream-owner claim (see streamOwnerRef) the user's Stop targets.
  // Stamped by handleStop with the claim that is live at that instant; every
  // reader compares against its own turn's claim, so a stop can only ever
  // affect the turn it was aimed at. This is what makes Stop locally
  // authoritative: a failed durable cancel no longer un-stops the turn, and a
  // stale stop can never leak into the next turn (its claim won't match).
  const stopTargetRef = useRef<number | null>(null);
  // Citations snapshot taken by handleStop the instant the user aborts —
  // storeStopStreaming() clears streamingCitations synchronously, but the
  // completion path still needs them to commit + persist the stopped answer's
  // sources. Cleared once the turn is finalized.
  const stopCitationsRef = useRef<Array<Record<string, unknown>>>([]);
  // Workspace thread id of the in-flight run, so Stop can close out the agent
  // activity indicator (the normal onDone never fires on abort).
  const activeRunThreadRef = useRef<string | null>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);
  const submitLockRef = useRef(false);
  // Monotonic claim on the store's live-streaming slice. runStreamTurn and
  // the confirm-resume path are separate stream owners that can overlap
  // across an await (approve lands while the paused turn is still
  // reconciling), so each stamps a token and only tears the slice down
  // while it still holds the claim.
  const streamOwnerRef = useRef(0);
  // CX1 belt: isConfirming (React state) is not synchronous, so two
  // Approve clicks in the same tick both see isConfirming === false before
  // either setState flushes. A ref mirrors submitLockRef's guard so a
  // double-click can't fire streamConfirm twice client-side (the server
  // now also claims atomically — this is defense in depth).
  const confirmLockRef = useRef(false);
  const streamingTimestampRef = useRef<number | null>(null);
  const streamingRafRef = useRef<number | null>(null);
  const pendingStreamContentRef = useRef<string | null>(null);
  const authRecoveryAttemptRef = useRef<StreamAuthRecoveryAttempt | null>(null);
  // rAF-batched SSE seq cursor (same pattern as streamingRafRef for tokens):
  // onSeq fires per frame, but the activity store only needs the latest value
  // once per paint.
  const seqRafRef = useRef<number | null>(null);
  // Carries the stream id alongside the cursor so the unmount flush can write
  // both without reaching into another ref from inside effect cleanup.
  const pendingSeqRef = useRef<{
    threadId: string;
    seq: number;
    streamId?: string;
  } | null>(null);
  // Run-correlation id per thread (envelope stream_id) — persisted with the
  // seq cursor so resume pins to the run the cursor came from.
  const streamIdByThreadRef = useRef<Record<string, string>>({});
  // Flush-then-cancel for the seq rAF at terminals: the last streamed seq is
  // the resume cursor, so a pending value must be committed synchronously —
  // cancelling the frame alone would drop it.
  const flushPendingSeq = useCallback(() => {
    if (seqRafRef.current !== null) {
      cancelAnimationFrame(seqRafRef.current);
      seqRafRef.current = null;
    }
    const p = pendingSeqRef.current;
    pendingSeqRef.current = null;
    if (p) {
      useAgentActivityStore
        .getState()
        .setStreamSeq(
          p.threadId,
          p.seq,
          streamIdByThreadRef.current[p.threadId] ?? p.streamId
        );
    }
  }, []);
  // Threads a resume was already attempted for this mount — guards against
  // double-resume from effect re-runs (StrictMode, dep changes).
  const resumeTriedRef = useRef<Set<string>>(new Set());
  // Threads whose LIVE turn died on a transport error. The backend run may
  // still be going, so the resume effect is allowed one reattach for these
  // even though the run has been finished as 'error' (server-authored error
  // frames deliberately do not land here — those turns really are over).
  const transportFailureRef = useRef<Set<string>>(new Set());
  // Threads already asked whether they hold a parked HITL confirmation —
  // one probe per thread activation (see the cold-load effect below).
  const confirmationProbedRef = useRef<Set<string>>(new Set());
  const lastActivatedThreadRef = useRef<string | null>(null);
  const probeAbortRef = useRef<AbortController | null>(null);
  const hadPendingApprovalRef = useRef(false);

  // ---- Store bindings ----
  const storeStopStreaming = useChatStore((state) => state.stopStreaming);
  const storeIsStreaming = useChatStore((state) => state.isStreaming);
  const storeStreamingContent = useChatStore((state) => state.streamingContent);
  const storeIsRetrievingRag = useChatStore((state) => state.isRetrievingRag);
  const streamingThreadId = useChatStore((state) => state.streamingThreadId);
  const activeThreadId = useChatStore((state) => state.currentThreadId);
  const authIsAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const authenticatedUserId = useAuthStore((state) => state.user?.id ?? null);
  const pendingConfirmation = activeThreadId
    ? (pendingConfirmations[activeThreadId] ?? null)
    : null;
  // `ownerThreadId` names the thread the confirmation belongs to. It matters
  // on the clear path: falling back to the *viewed* thread deleted the wrong
  // key whenever the user switched threads mid-confirm, stranding a settled
  // approval card (and a locked composer) on the original thread forever.
  const setPendingConfirmation = useCallback(
    (next: PendingConfirmation | null, ownerThreadId?: string | null) => {
      const threadId =
        next?.workspaceThreadId || ownerThreadId || activeThreadId;
      if (!threadId) return;
      setPendingConfirmations((current) => {
        if (next) return { ...current, [threadId]: next };
        if (!(threadId in current)) return current;
        const updated = { ...current };
        delete updated[threadId];
        return updated;
      });
    },
    [activeThreadId]
  );

  const router = useRouter();
  const queryClient = useQueryClient();

  const finishAuthRecoveryAttempt = useCallback(
    (attempt: StreamAuthRecoveryAttempt | undefined): void => {
      if (!attempt) return;
      clearArmedChatAuthRecovery(attempt.attemptId);
      if (authRecoveryAttemptRef.current === attempt) {
        authRecoveryAttemptRef.current = null;
      }
    },
    []
  );

  const beginAuthRecovery = useCallback(
    (attempt: StreamAuthRecoveryAttempt): void => {
      if (
        authRecoveryAttemptRef.current !== attempt ||
        attempt.navigationStarted
      ) {
        return;
      }

      const auth = useAuthStore.getState();
      // The response belongs to the snapshotted user. If another account has
      // already replaced it, discard that user's staged text and leave the
      // newer session completely untouched.
      if (
        auth.user &&
        (attempt.ownerUserId === null || auth.user.id !== attempt.ownerUserId)
      ) {
        if (attempt.promptStaged) {
          discardChatAuthRecovery(attempt.attemptId);
        }
        authRecoveryAttemptRef.current = null;
        return;
      }

      const draftSaved =
        attempt.promptStaged && markChatAuthRecoveryReady(attempt.attemptId);
      // Latch before mutating auth. The synchronous store update rerenders the
      // hook and its recovery effect, which must not issue a second replace.
      attempt.navigationStarted = true;

      if (attempt.threadId) {
        useAgentActivityStore.getState().finishRun(attempt.threadId, 'error');
      }

      if (auth.isAuthenticated) {
        auth.invalidateRejectedSession(attempt.ownerUserId);
      }

      const nextPath = attempt.threadId
        ? getSelectedThreadUrl(attempt.threadId)
        : '/chat';
      const recoveryParams = new URLSearchParams();
      recoveryParams.set('reauth', 'chat');
      if (draftSaved) recoveryParams.set('draft', 'saved');
      recoveryParams.set('next', nextPath);
      router.replace(`/login?${recoveryParams.toString()}`);
    },
    [router]
  );

  // Recovery is once per activation, not once per component lifetime. A
  // failed resume/probe can be retried by leaving and returning to the thread.
  useEffect(() => {
    if (lastActivatedThreadRef.current !== activeThreadId) {
      if (activeThreadId) {
        resumeTriedRef.current.delete(activeThreadId);
        confirmationProbedRef.current.delete(activeThreadId);
      }
      lastActivatedThreadRef.current = activeThreadId;
    }
  }, [activeThreadId]);

  // A rejected Supabase refresh can emit SIGNED_OUT before the retried fetch
  // settles. Promote the already-staged draft and navigate here, while the
  // routed hook is still mounted; its later cleanup may then abort the fetch
  // without losing the handoff.
  useEffect(() => {
    if (authIsAuthenticated) return;
    const attempt = authRecoveryAttemptRef.current;
    if (!attempt?.authRefreshStarted) return;
    beginAuthRecovery(attempt);
  }, [authIsAuthenticated, beginAuthRecovery]);

  // Recovery is composer-only. Consumption removes the record first, then
  // restores text for the same account and thread without dispatching Send.
  useEffect(() => {
    const recoveryThreadId = authRecoveryRoute?.threadId;
    if (
      !authRecoveryRoute?.isReady ||
      !authIsAuthenticated ||
      !authenticatedUserId ||
      !recoveryThreadId
    ) {
      return;
    }
    const liveAuth = useAuthStore.getState();
    if (
      !liveAuth.isAuthenticated ||
      liveAuth.user?.id !== authenticatedUserId ||
      useChatStore.getState().currentThreadId !== recoveryThreadId
    ) {
      return;
    }
    const restoredPrompt = consumeChatAuthRecovery({
      ownerUserId: authenticatedUserId,
      threadId: recoveryThreadId,
    });
    if (restoredPrompt !== null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setInput(restoredPrompt);
    }
  }, [authIsAuthenticated, authenticatedUserId, authRecoveryRoute]);

  // Refetch the Working-folders queries once a project-mutating tool
  // succeeds, so the rail shows agent-created sources/notes/drafts without
  // waiting out the 5-minute staleTime.
  const invalidateProjectDataForTool = useCallback(
    (tool: string, isError: boolean) => {
      if (isError || !PROJECT_MUTATING_TOOLS.has(tool)) return;
      // Scope to the bound project so we don't invalidate every
      // ['project', …] query (project list, unrelated project details,
      // metadata). Fall back to broad invalidation when no project is
      // bound (global chat has no narrower key to target).
      void queryClient.invalidateQueries({
        queryKey: boundProjectId ? ['project', boundProjectId] : ['project'],
      });
    },
    [queryClient, boundProjectId]
  );

  // Auto-focus a note the agent just created in the split-view artifact
  // panel. Guards: only successful create_project_note (create_draft is an
  // async task — there is no draft id at tool_end), only when the turn's
  // thread is the one on screen (a background thread must not hijack the
  // panel), only when a project is bound (the note fetch needs its id), and
  // openArtifact's own pin gate ignores agent opens while the user has
  // pinned what they're reading.
  const maybeAutoFocusCreatedNote = useCallback(
    (
      tool: string,
      result: string,
      isError: boolean,
      turnThreadId: string | null
    ) => {
      if (isError || tool !== 'create_project_note') return;
      if (
        !turnThreadId ||
        useChatStore.getState().currentThreadId !== turnThreadId
      )
        return;
      const created = parseCreatedNoteResult(result);
      if (!created) return;
      // Prefer the project id the backend actually created the note in —
      // an explicit project arg can differ from the thread's binding, and
      // fetching through the wrong project 404s. Bound project is only a
      // fallback for older payloads without project_id.
      const noteProjectId = created.projectId ?? boundProjectId;
      if (!noteProjectId) return;
      useArtifactPanelStore.getState().openArtifact(
        {
          kind: 'note',
          projectId: noteProjectId,
          id: created.noteId,
          title: created.title ?? 'New note',
        },
        { source: 'agent' }
      );
    },
    [boundProjectId]
  );

  // ---- Effects ----

  // Capture a stable timestamp when streaming begins
  useEffect(() => {
    if (storeIsStreaming) {
      streamingTimestampRef.current = Date.now();
    }
  }, [storeIsStreaming]);

  // Cleanup RAF on unmount
  useEffect(() => {
    return () => {
      if (streamingRafRef.current !== null) {
        cancelAnimationFrame(streamingRafRef.current);
      }
      if (seqRafRef.current !== null) {
        cancelAnimationFrame(seqRafRef.current);
        // The cursor update was throttled through that rAF: dropping it here
        // makes the next resume replay from an older seq than we actually saw.
        const pending = pendingSeqRef.current;
        if (pending) {
          // Carry the stream id too, exactly as the rAF callbacks do: without
          // it a resume after the original stream finished has nothing to
          // point at, gets a 204, and closes the run without the answer.
          useAgentActivityStore
            .getState()
            .setStreamSeq(pending.threadId, pending.seq, pending.streamId);
        }
      }
      pendingSeqRef.current = null;
      // Abort this instance's in-flight SSE fetch. The backend run survives a
      // client disconnect (buffered stream) and the mount-time resume effect
      // reattaches. Leaving the fetch orphaned instead kept the stream owned
      // by a dead hook instance: the remounted Stop button's abortControllerRef
      // is null and could never stop it.
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;
    };
  }, []);

  // ---- Handlers ----

  /**
   * Run one agent turn end-to-end — streaming state setup, the single shared
   * set of SSE callbacks, and the commit/persist/cleanup path. Used by both
   * handleSubmit (POST /agent/stream) and the mount-time resume effect
   * (GET /agent/stream/resume) so a resumed stream renders through EXACTLY
   * the same path as a live one (isStreaming cleared BEFORE setMessages).
   */
  const runStreamTurn = useCallback(
    async (opts: {
      currentThreadId: string | null;
      currentConversationId: string | null;
      newMessages: ChatPageMessage[];
      assistantRuntimeId: string;
      /** Resume: a replay that yields no tokens (nothing buffered / 204)
       * must unwind quietly instead of rendering a "no response" bubble. */
      quietWhenEmpty?: boolean;
      /** Resume: consulted before committing the rebuilt answer. A replay that
       * started past our cursor is missing a prefix, so the local
       * reconstruction must not be shown as this turn's answer — the reconcile
       * that follows brings the complete server row instead. */
      suppressCommit?: () => boolean;
      /** Resume: a failed reattach must leave the activity run 'running' so
       * re-activating the thread can try again — the run itself may well
       * still be alive on the backend. */
      keepRunOnFailure?: boolean;
      authRecoveryAttempt?: StreamAuthRecoveryAttempt;
      start: (
        callbacks: AgentStreamCallbacks,
        signal: AbortSignal
      ) => Promise<void>;
    }): Promise<void> => {
      const {
        currentThreadId,
        currentConversationId,
        newMessages,
        assistantRuntimeId,
        quietWhenEmpty,
        suppressCommit,
        keepRunOnFailure,
        authRecoveryAttempt,
        start,
      } = opts;

      // Claim the store's streaming slice for this turn (see streamOwnerRef).
      const streamOwner = (streamOwnerRef.current += 1);
      const isStoppedByUser = (): boolean =>
        stopTargetRef.current === streamOwner;

      // The thread this turn belongs to, snapshotted after thread creation.
      // Every local setMessages below must be gated on the user still viewing
      // this thread: setMessages writes to whatever thread is CURRENTLY
      // displayed, and the sidebar switches threads without aborting the
      // stream. Store/back-end persistence is thread-scoped already, so a
      // skipped local write is not lost — it reappears when the user returns.
      // ponytail: the global streaming bubble/flags still render on whatever
      // thread is displayed while a background turn streams — per-thread
      // streaming state is the upgrade path if that becomes noticeable.
      const turnThreadId = currentThreadId;
      const isTurnDisplayed = () =>
        useChatStore.getState().currentThreadId === turnThreadId;
      const userRuntimeId = [...newMessages]
        .reverse()
        .find(
          (message) =>
            message.role === 'user' && message.source !== 'local-only'
        )?.runtimeId;
      const reconciliationDiagnostic = (terminalReason: string) => ({
        terminalReason,
        localCount: newMessages.length,
        completedInBackground: !isTurnDisplayed(),
      });
      const reconcileUser = (terminalReason: string) =>
        turnThreadId
          ? useChatStore.getState().refreshMessages(
              turnThreadId,
              userRuntimeId
                ? {
                    runtimeId: userRuntimeId,
                    diagnostic: reconciliationDiagnostic(terminalReason),
                  }
                : undefined
            )
          : Promise.resolve(false);
      const reconcileAssistant = (
        doneIds: {
          assistant_message_id?: string | null;
          client_message_id?: string | null;
        },
        terminalReason: string
      ) =>
        turnThreadId
          ? useChatStore.getState().refreshMessages(turnThreadId, {
              persistedId: doneIds.assistant_message_id ?? undefined,
              runtimeId: doneIds.client_message_id ?? assistantRuntimeId,
              diagnostic: reconciliationDiagnostic(terminalReason),
            })
          : Promise.resolve(false);

      if (turnThreadId) {
        useChatStore.getState().markMessagesStale(turnThreadId);
      }

      // Persisted identities arrive with the terminal done event. Keep this
      // outside the try block so abort/error reconciliation can fall back to
      // the deterministic runtime identity when no done frame arrived.
      let doneIds: {
        assistant_message_id?: string | null;
        client_message_id?: string | null;
        tool_executions?: Array<Record<string, unknown>>;
        progress_steps?: AgentProgressStep[];
      } = {};

      try {
        let assistantContent = '';
        lastStreamedContentRef.current = '';
        let streamHadError = false;
        let streamHadConfirmation = false;
        const responseStart = Date.now();
        // Stamped on the first token so the committed bubble can split the
        // clock into working vs writing straight away. The backend persists
        // its own reading (chat_messages.ttft_ms) for the post-reload row —
        // this mirrors responseTimeMs, which is likewise client-measured
        // in-session and server-measured after a reload.
        let firstTokenAt: number | null = null;

        // Per-turn step tracking — reset each send
        const turnSteps: ActivityStep[] = [];
        // Structured plan snapshot for the committed message (the activity
        // store only keeps flattened strings for the ContextRail).
        let turnPlan: PlanStep[] = [];
        // Planner's top-level rationale for turnPlan, carried the same way.
        let turnPlanReasoning = '';
        let turnProgress: AgentProgressStep[] = [];
        let turnReasoning = '';
        // One entry per invocation: two parallel calls to the same tool used
        // to share a slot, so the second end read the first's start time.
        const toolStartTimes = new Map<string, number[]>();
        // Per-turn LLM token usage, captured from the `usage` SSE event that
        // fires just before `done`. Null until (and unless) it arrives.
        let turnTokenUsage: { input: number; output: number } | null = null;

        // Set streaming state in store for UI. streamingThreadId records
        // WHICH thread owns this live turn (CX5) — isStreaming etc. stay
        // global (single-flight is unchanged), but a thread-scoped consumer
        // can gate on streamingThreadId === its own activeThreadId so a
        // background turn's live tokens/citations don't render on whatever
        // thread the user has switched to.
        useChatStore.setState({
          isStreaming: true,
          streamingContent: '',
          streamingSteps: [],
          streamingPlan: [],
          streamingProgress: [],
          streamingReasoning: '',
          // Only "retrieving" when RAG is on; cleared on first token / context.
          isRetrievingRag: enableRAG,
          // Fresh turn — drop the previous turn's heartbeat reading.
          streamingElapsedMs: null,
          streamingPhase: 'accepted',
          streamingStatusDetail: null,
          streamingThreadId: turnThreadId,
        });

        // Render the in-flight turn as a real placeholder message in the
        // transcript (its live text/steps/citations are read from the
        // streaming store by AuiAssistantMessage). The placeholder is replaced
        // by the committed message on `done`, or removed at every early exit
        // below.
        if (isTurnDisplayed()) {
          const placeholder: ChatPageMessage = {
            runtimeId: assistantRuntimeId,
            source: 'optimistic',
            role: 'assistant',
            content: '',
            timestamp: Date.now(),
            isStreaming: true,
          };
          setMessages([...newMessages, placeholder]);
        }

        // Fresh turn — record the thread so Stop can finalize this run's
        // activity indicator. (No stop-flag reset needed: a previous turn's
        // stop target can't match this turn's claim.)
        activeRunThreadRef.current = currentThreadId || null;

        const streamAbort = new AbortController();
        abortControllerRef.current = streamAbort;

        await start(
          {
            onToken: (content) => {
              if (firstTokenAt === null) firstTokenAt = Date.now();
              assistantContent += content;
              lastStreamedContentRef.current = assistantContent;
              pendingStreamContentRef.current = assistantContent;
              if (streamingRafRef.current === null) {
                streamingRafRef.current = requestAnimationFrame(() => {
                  streamingRafRef.current = null;
                  if (pendingStreamContentRef.current !== null) {
                    useChatStore.setState({
                      streamingContent: pendingStreamContentRef.current,
                      isRetrievingRag: false,
                    });
                    pendingStreamContentRef.current = null;
                  }
                });
              }
            },
            onReasoningDelta: (content) => {
              turnReasoning = (turnReasoning + content).slice(
                0,
                MAX_REASONING_SUMMARY_CHARS
              );
              useChatStore.setState({ streamingReasoning: turnReasoning });
            },
            onHeartbeat: (elapsedMs) => {
              // The only progress signal during a long silent planner/LLM
              // phase — used to correct the live MessageTiming clock.
              useChatStore.setState({ streamingElapsedMs: elapsedMs });
            },
            onStatus: (phase, detail) => {
              turnProgress = appendProgressStep(turnProgress, phase, detail);
              useChatStore.setState({
                streamingPhase: phase,
                streamingStatusDetail: detail ?? null,
                streamingProgress: [...turnProgress],
              });
            },
            onStreamId: (sid) => {
              if (!currentThreadId) return;
              streamIdByThreadRef.current[currentThreadId] = sid;
            },
            onSeq: (seq) => {
              if (!currentThreadId) return;
              pendingSeqRef.current = {
                threadId: currentThreadId,
                seq,
                streamId: streamIdByThreadRef.current[currentThreadId],
              };
              if (seqRafRef.current === null) {
                seqRafRef.current = requestAnimationFrame(() => {
                  seqRafRef.current = null;
                  const p = pendingSeqRef.current;
                  pendingSeqRef.current = null;
                  if (p) {
                    useAgentActivityStore
                      .getState()
                      .setStreamSeq(
                        p.threadId,
                        p.seq,
                        streamIdByThreadRef.current[p.threadId]
                      );
                  }
                });
              }
            },
            onToolStart: (tool, args, callId) => {
              console.log('[Agent] Tool start:', tool, args);
              if (currentThreadId) {
                useAgentActivityStore
                  .getState()
                  .pushToolStart(currentThreadId, tool, callId);
              }
              // Per-turn tracking for inline activity strip
              const invocationKey = toolInvocationKey(tool, callId);
              toolStartTimes.set(invocationKey, [
                ...(toolStartTimes.get(invocationKey) ?? []),
                Date.now(),
              ]);
              turnSteps.push({
                ...(callId ? { id: callId } : {}),
                tool,
                label: toolLabel(tool),
                status: 'running',
                argsSummary: summarizeToolArgs(args),
                ...(args && typeof args === 'object' ? { args } : {}),
              });
              useChatStore.setState({
                streamingSteps: [...turnSteps],
                streamingStatusDetail: toolStatusLabel(tool, 'active'),
              });
            },
            onToolEnd: (tool, result, isError, callId) => {
              console.log('[Agent] Tool end:', tool, result, { isError });
              if (currentThreadId) {
                useAgentActivityStore
                  .getState()
                  .pushToolEnd(currentThreadId, tool, !isError, callId);
              }
              invalidateProjectDataForTool(tool, isError);
              maybeAutoFocusCreatedNote(
                tool,
                result,
                isError,
                currentThreadId || null
              );
              // Update last matching running step for this tool
              // Newest-first, matching the newest running step settled below.
              const invocationKey = toolInvocationKey(tool, callId);
              const startTime = toolStartTimes.get(invocationKey)?.pop();
              const durationMs = startTime ? Date.now() - startTime : undefined;
              const idx = findRunningToolStepIndex(turnSteps, tool, callId);
              if (idx !== undefined) {
                turnSteps[idx] = {
                  ...turnSteps[idx],
                  status: isError ? 'error' : 'done',
                  durationMs,
                  resultSummary: summarizeToolResult(result),
                  result,
                };
              }
              useChatStore.setState({
                streamingSteps: [...turnSteps],
                streamingStatusDetail: toolStatusLabel(
                  tool,
                  isError ? 'error' : 'done'
                ),
              });
            },
            onRagContext: (contexts) => {
              console.log('[Agent] RAG contexts:', contexts.length);
              useChatStore.setState({
                streamingCitations: contexts,
                isRetrievingRag: false,
                streamingStatusDetail: `Reading ${contexts.length} ${
                  contexts.length === 1 ? 'source' : 'sources'
                }`,
              });
            },
            onPlan: (steps, reasoning) => {
              // Structured copy for the inline transcript plan — keeps
              // tool/depends_on so status derivation works after commit.
              turnPlan = toTurnPlan(steps);
              turnPlanReasoning = reasoning;
              // Live copy for the in-flight transcript row (AuiStreamingBody)
              // — lets the plan render WHILE the turn streams, not only
              // after commit. Plan events fire once per run, not per token.
              useChatStore.setState({
                streamingPlan: [...turnPlan],
                streamingStatusDetail: 'Working through the plan',
              });
              if (!currentThreadId) return;
              const items = toActivityPlanItems(steps);
              if (items.length > 0) {
                useAgentActivityStore
                  .getState()
                  .setPlan(currentThreadId, items);
              }
            },
            onUsage: (inputTokens, outputTokens) => {
              turnTokenUsage = { input: inputTokens, output: outputTokens };
            },
            onReflection: (_passed, _issues, _round, revising) => {
              if (!revising) return;
              assistantContent = '';
              lastStreamedContentRef.current = '';
              pendingStreamContentRef.current = '';
              useChatStore.setState({ streamingContent: '' });
            },
            onConfirmation: (threadId, confirmation) => {
              console.log('[Agent] HITL confirmation needed:', confirmation);
              streamHadConfirmation = true;
              setPendingConfirmation({
                threadId,
                workspaceThreadId: currentThreadId || '',
                approvalId: crypto.randomUUID(),
                confirmation,
                // Only settled steps: the interrupted tool re-emits its own
                // tool_start on resume, so a carried 'running' step would
                // duplicate it.
                steps: turnSteps.filter((s) => s.status !== 'running'),
                plan: [...turnPlan],
                planReasoning: turnPlanReasoning || undefined,
                progress: [...turnProgress],
                // Snapshot NOW — the streamHadConfirmation exit below clears
                // streamingCitations before the confirm stream starts.
                citations: useChatStore.getState().streamingCitations,
                userRuntimeId,
                assistantRuntimeId,
              });
            },
            onAuthRefreshAttempt: () => {
              if (
                authRecoveryAttemptRef.current === authRecoveryAttempt &&
                authRecoveryAttempt
              ) {
                authRecoveryAttempt.authRefreshStarted = true;
              }
            },
            onAuthRefreshSuccess: () => {
              if (
                authRecoveryAttemptRef.current === authRecoveryAttempt &&
                authRecoveryAttempt
              ) {
                authRecoveryAttempt.authRefreshStarted = false;
              }
            },
            onDone: (payload) => {
              console.log('[Agent] Stream complete');
              finishAuthRecoveryAttempt(authRecoveryAttempt);
              if (payload) {
                doneIds = payload;
              }
              if (currentThreadId) {
                useAgentActivityStore
                  .getState()
                  .finishRun(currentThreadId, 'done');
              }
            },
            onError: (error, category, localFailure) => {
              console.error(
                '[Agent] Stream error:',
                error,
                category,
                localFailure
              );
              streamHadError = true;
              if (currentThreadId) {
                useAgentActivityStore
                  .getState()
                  .finishRun(currentThreadId, 'error');
              }
              if (
                localFailure === 'authentication_required' &&
                authRecoveryAttempt
              ) {
                beginAuthRecovery(authRecoveryAttempt);
                return;
              }
              finishAuthRecoveryAttempt(authRecoveryAttempt);
              // Show error as assistant message instead of blank bubble.
              // The server authored this failure, so its category wins; the
              // legacy client-side 'stream-error' is only the fallback for a
              // backend that does not send one yet (or a transport failure,
              // where the service deliberately reports no category).
              const errorMsg: ChatPageMessage = {
                runtimeId: crypto.randomUUID(),
                source: 'local-only',
                role: 'assistant',
                content: `Stream error: ${error}`,
                timestamp: Date.now(),
                error: {
                  message:
                    'This response failed to generate. Please try again.',
                  category: localFailure ?? category ?? 'stream-error',
                },
              };
              if (isTurnDisplayed())
                setMessages(
                  replacePreservingApproval([...newMessages, errorMsg])
                );
            },
          },
          streamAbort.signal
        );

        // Cancel any pending RAF flush — streaming is done
        if (streamingRafRef.current !== null) {
          cancelAnimationFrame(streamingRafRef.current);
          streamingRafRef.current = null;
        }
        flushPendingSeq();
        pendingStreamContentRef.current = null;

        // Don't append a normal message if stream errored or needs confirmation
        if (streamHadError || streamHadConfirmation) {
          // On a confirmation pause the placeholder must go (P4 re-introduces
          // it as an in-band approval part). On error, onError already replaced
          // the placeholder with the error bubble — leave it.
          if (streamHadConfirmation && !streamHadError && isTurnDisplayed()) {
            setMessages(replacePreservingApproval(newMessages));
          }
          if (streamHadConfirmation && currentThreadId) {
            // The stream really did end; the run is parked on the user, not
            // running. Leaving it 'running' kept the activity rail spinning
            // and let the resume effect fire the moment isLoading cleared.
            useAgentActivityStore
              .getState()
              .finishRun(currentThreadId, 'stopped');
          }
          resetStreamingTurnState();
          await reconcileUser(
            streamHadConfirmation ? 'confirmation-paused' : 'stream-error'
          );
          setIsLoading(false);
          return;
        }

        // Guard against empty content (no tokens received). Surface the failure
        // to the user instead of leaving an empty bubble — every "agent broke
        // upstream" failure (DNS, auth, model error) used to look identical from
        // the UI side.
        const finalContent = assistantContent || lastStreamedContentRef.current;
        if (!finalContent.trim()) {
          // A user-stop before the first token: just unwind quietly — no error
          // bubble for an answer the user chose not to wait for.
          if (!isStoppedByUser() && !quietWhenEmpty) {
            const emptyResponseMessage: ChatPageMessage = {
              runtimeId: crypto.randomUUID(),
              source: 'local-only',
              role: 'assistant',
              content: '',
              timestamp: Date.now(),
              error: {
                message: 'No response was received. Please try again.',
                category: 'empty-response',
              },
            };
            if (isTurnDisplayed())
              setMessages([...newMessages, emptyResponseMessage]);
          } else if (isTurnDisplayed()) {
            // Quiet/stopped unwind with no content: drop the placeholder so no
            // empty streaming bubble is left behind — but keep any pending
            // approval, which this path used to delete on every HITL pause.
            setMessages(replacePreservingApproval(newMessages));
          }
          resetStreamingTurnState();
          if (isStoppedByUser()) {
            await reconcileAssistant(doneIds, 'stopped-before-token');
          } else {
            await reconcileUser('empty-response');
          }
          setIsLoading(false);
          if (isStoppedByUser()) stopTargetRef.current = null;
          activeRunThreadRef.current = null;
          return;
        }

        // After streaming completes, build final message.
        // Clear the streaming state BEFORE appending the final assistant message
        // so the virtual streaming bubble unmounts atomically with the real one
        // mounting. Otherwise the final message and the streaming bubble render
        // together during the (awaited) DB save window below.
        const responseTimeMs = Date.now() - responseStart;
        const wasStopped = isStoppedByUser();
        // The done payload carries the graph state's tool executions for the
        // WHOLE turn (parsed results, real durations) — richer than the live
        // SSE summaries and identical to what a reload shows. Prefer them so
        // the committed bubble renders tools even if a live tool_start/tool_end
        // frame was dropped on the wire (previously that left tools missing
        // until a page refresh). Fall back to live steps for legacy servers.
        const serverSteps = mapDbToolExecutions(
          doneIds.tool_executions as DbToolExecution[] | undefined
        );
        const finalTurnSteps =
          serverSteps && serverSteps.length > 0 ? serverSteps : [...turnSteps];
        // Capture the turn's RAG citations BEFORE the streaming state is
        // cleared below — they are attached to the committed message (so
        // inline [Doc N] refs keep resolving after the stream ends) and
        // persisted with the assistant row (so they survive reload).
        // On a user Stop, storeStopStreaming() has ALREADY wiped
        // streamingCitations out-of-band — handleStop snapshots them into
        // stopCitationsRef first, so a stopped RAG answer keeps its sources.
        const liveCitations = useChatStore.getState().streamingCitations;
        const turnCitations =
          liveCitations.length > 0
            ? liveCitations
            : wasStopped
              ? stopCitationsRef.current
              : liveCitations;
        stopCitationsRef.current = [];
        const finalAssistantMessage: ChatPageMessage = {
          runtimeId: assistantRuntimeId,
          source: 'optimistic',
          role: 'assistant',
          content: finalContent,
          timestamp: Date.now(),
          citations:
            turnCitations.length > 0
              ? turnCitations.map(normalizeCitation)
              : undefined,
          toolExecutions:
            finalTurnSteps.length > 0 ? finalTurnSteps : undefined,
          plan: turnPlan.length > 0 ? turnPlan : undefined,
          planReasoning: turnPlanReasoning || undefined,
          progressSteps:
            doneIds.progress_steps && doneIds.progress_steps.length > 0
              ? doneIds.progress_steps
              : turnProgress.length > 0
                ? turnProgress
                : undefined,
          metadata: {
            responseTimeMs,
            ...(firstTokenAt !== null
              ? { ttftMs: firstTokenAt - responseStart }
              : {}),
            ...(wasStopped ? { stopped: true } : {}),
            ...(finalTurnSteps.length > 0
              ? {
                  toolsUsed: finalTurnSteps.map((s) => s.label),
                }
              : {}),
            ...(turnCitations.length > 0
              ? { sourcesCount: turnCitations.length }
              : {}),
            ...(turnTokenUsage ? { tokenUsage: turnTokenUsage } : {}),
          },
        };
        if (isStoppedByUser()) stopTargetRef.current = null;
        activeRunThreadRef.current = null;

        resetStreamingTurnState();
        lastStreamedContentRef.current = '';

        // The BACKEND is the sole message writer (server-canonical): it
        // persisted both rows (user pre-stream, assistant before `done` —
        // full fidelity incl. tool_executions), so the client only reconciles
        // its optimistic bubble with the persisted id.
        const reconciledAssistantMessage = doneIds.assistant_message_id
          ? { ...finalAssistantMessage, id: doneIds.assistant_message_id }
          : finalAssistantMessage;
        // A turn that failed mid-stream and then recovered (the resume effect
        // re-attaches and commits the real answer) would otherwise render the
        // transient "Something went wrong" bubble ABOVE its own answer until
        // the next reload — the recovery snapshot includes it.
        const finalMessages = [
          ...newMessages.filter(
            (message) =>
              !(message.source === 'local-only' && message.error !== undefined)
          ),
          reconciledAssistantMessage,
        ];
        if (isTurnDisplayed() && !suppressCommit?.()) {
          setMessages(finalMessages);
        }

        // Conversation state is sidebar metadata only. The transcript remains
        // the Zustand canonical page plus this hook's local overlay.
        setConversations((prev) =>
          prev.map((conv) =>
            conv.id === currentConversationId
              ? {
                  ...conv,
                  messages: [],
                  previewText: finalContent,
                  messageCount: Math.max(
                    conv.messageCount ?? 0,
                    displayedMessages.length + 2
                  ),
                  updatedAt: Date.now(),
                }
              : conv
          )
        );
        await reconcileAssistant(doneIds, wasStopped ? 'stopped' : 'done');
      } catch (err) {
        // A user stop should never read as a failure. (streamMessage already
        // swallows AbortError, but guard here too in case the abort surfaces.)
        if (!isStoppedByUser()) {
          console.error('Failed to send message:', err);

          if (isTurnDisplayed())
            setMessages([
              ...newMessages,
              {
                runtimeId: crypto.randomUUID(),
                source: 'local-only',
                role: 'assistant',
                content: '',
                timestamp: Date.now(),
                error: {
                  message:
                    'Something went wrong sending your message. Please try again.',
                  category: 'exception',
                },
              },
            ]);
        }
        // onDone/onError never fired for a thrown transport error, so the
        // activity run stayed 'running' forever — the rail kept spinning and
        // recovery depended on the resume effect re-firing by accident.
        const failedRunThread = activeRunThreadRef.current ?? currentThreadId;
        if (failedRunThread && !keepRunOnFailure && !isStoppedByUser()) {
          transportFailureRef.current.add(failedRunThread);
        }
        if (failedRunThread && !keepRunOnFailure) {
          useAgentActivityStore
            .getState()
            .finishRun(
              failedRunThread,
              isStoppedByUser() ? 'stopped' : 'error'
            );
        }
        if (isStoppedByUser()) {
          await reconcileAssistant(doneIds, 'abort');
        } else {
          await reconcileUser('exception');
        }
      } finally {
        finishAuthRecoveryAttempt(authRecoveryAttempt);
        submitLockRef.current = false;
        setIsLoading(false);
        // A token that landed just before an exception can leave a scheduled
        // rAF behind; it would fire after this teardown and write stale
        // streamingContent back into the store. Scoped to the owner: the rAF
        // slot is shared, so cancelling it while a confirm stream owns the
        // slice would drop that stream's first flush.
        if (
          streamOwnerRef.current === streamOwner &&
          streamingRafRef.current !== null
        ) {
          cancelAnimationFrame(streamingRafRef.current);
          streamingRafRef.current = null;
        }
        if (streamOwnerRef.current === streamOwner) {
          flushPendingSeq();
        }
        if (streamOwnerRef.current === streamOwner) {
          pendingStreamContentRef.current = null;
        }
        // Only tear the slice down if nothing newer claimed it. On a
        // confirmation pause the awaits above hand the user a live approval
        // card; approving starts the confirm stream, and an unconditional wipe
        // here orphaned it — streaming UI gone, carried citations lost,
        // composer unlocked into a second concurrent SSE writer.
        if (streamOwnerRef.current === streamOwner) {
          resetStreamingTurnState();
        }
        if (streamOwnerRef.current === streamOwner) {
          // Same ownership rule: these refs are shared with the confirm
          // stream; a newer owner's state must not be torn down here.
          lastStreamedContentRef.current = '';
        }
        if (stopTargetRef.current === streamOwner) {
          stopTargetRef.current = null;
        }
        activeRunThreadRef.current = null;
      }
    },
    [
      flushPendingSeq,
      beginAuthRecovery,
      finishAuthRecoveryAttempt,
      setMessages,
      setConversations,
      enableRAG,
      invalidateProjectDataForTool,
      maybeAutoFocusCreatedNote,
      setPendingConfirmation,
      displayedMessages.length,
    ]
  );

  const handleSubmit = useCallback(
    async (
      contentOverride?: string,
      historyOverride?: ChatPageMessage[],
      supersedesClientMessageId?: string,
      attachmentIds?: string[]
    ) => {
      // This identity must be captured before the stream's token refresh can
      // synchronously emit SIGNED_OUT and clear the auth store.
      const ownerUserId = useAuthStore.getState().user?.id;
      if (submitLockRef.current) {
        console.warn('[Chat] Send ignored: submit already in flight');
        return;
      }
      const rawContent =
        typeof contentOverride === 'string' ? contentOverride : input;
      const content = rawContent.trim();
      if (!content || isLoading || storeIsStreaming) {
        console.warn('[Chat] Send ignored by submit guard', {
          empty: !content,
          isLoading,
          storeIsStreaming,
        });
        return;
      }
      submitLockRef.current = true;
      // Stop must also cover the first-send setup awaits before runStreamTurn
      // installs the live stream controller.
      const preflightAbort = new AbortController();
      abortControllerRef.current = preflightAbort;
      const ownsPreflight = (): boolean =>
        useAuthStore.getState().user?.id === ownerUserId;
      const abandonPreflight = (): void => {
        setIsLoading(false);
        submitLockRef.current = false;
        stopTargetRef.current = null;
      };
      const rollbackPreflight = (): void => {
        if (!ownsPreflight()) {
          abandonPreflight();
          return;
        }
        setMessages(messages);
        setInput(content);
        abandonPreflight();
      };
      const preflightWasSuperseded = (): boolean => {
        if (preflightAbort.signal.aborted) return true;
        if (ownsPreflight()) return false;
        abandonPreflight();
        return true;
      };
      // A cold-load confirmation probe still in flight is now stale: whatever
      // interrupt it might replay predates this turn, and the server abandons
      // it when the new send arrives. Left running, its late confirmation
      // frame armed a phantom approval card and locked the composer.
      probeAbortRef.current?.abort();
      try {
        // Create the idempotency/runtime identity before the optimistic bubble.
        // The backend stores this on the user row and derives the assistant row's
        // client id with the same UUIDv5 contract below.
        const turnClientMessageId = crypto.randomUUID();
        const assistantRuntimeId = uuidv5(
          `nous-assistant:${turnClientMessageId}`,
          uuidv5.URL
        );
        const userMessage: ChatPageMessage = {
          runtimeId: turnClientMessageId,
          // Same value as runtimeId here, but recorded explicitly so an edit of
          // this still-optimistic turn can supersede it without guessing.
          clientMessageId: turnClientMessageId,
          source: 'optimistic',
          role: 'user',
          content,
          timestamp: Date.now(),
        };

        // CX2: build the turn from the RECONCILED view (local ∪ store) — the
        // local array is empty during the lazy-load window after a thread
        // switch, and submitting from it silently dropped the whole history.
        const requestHistory = historyOverride ?? displayedMessages;
        const requestMessages = [...requestHistory, userMessage];
        // Local state contains only rows the canonical page has not yet
        // absorbed. A retry/next send clears local-only error rows while keeping
        // still-unreconciled optimistic identities visible. Regeneration passes
        // an explicit history prefix, so optimistic rows after its cutoff must
        // not leak back into the regenerated request or local overlay.
        const retainedRuntimeIds = historyOverride
          ? new Set(requestHistory.map((message) => message.runtimeId))
          : null;
        const newMessages = [
          ...messages.filter(
            (message) =>
              message.source === 'optimistic' &&
              (!retainedRuntimeIds || retainedRuntimeIds.has(message.runtimeId))
          ),
          userMessage,
        ];
        setMessages(newMessages);
        setInput('');
        setIsLoading(true);
        preflightAbort.signal.addEventListener('abort', rollbackPreflight, {
          once: true,
        });

        // Create new thread if needed (when no active conversation).
        // Read from ref first (synchronous, immune to React batching), then
        // state, then Zustand store as final fallback.
        let currentConversationId = useChatStore.getState().currentThreadId;
        let currentThreadId = currentConversationId;
        let activeWorkspaceId = workspace?.id ?? dbConversation?.workspace_id;

        if (!currentConversationId) {
          try {
            // Warm-start resolves dbConversation off the paint path. A user can
            // begin a fresh chat before that request settles, so recover it here
            // before creating the thread instead of streaming with no thread ID.
            let threadConversation = dbConversation;
            if (!threadConversation) {
              if (!activeWorkspaceId) {
                const defaultWorkspace =
                  await workspaceService.getOrCreateDefaultWorkspace();
                if (preflightWasSuperseded()) return;
                activeWorkspaceId = defaultWorkspace.id;
              }
              threadConversation =
                await workspaceService.getOrCreateDefaultConversation(
                  activeWorkspaceId
                );
              if (preflightWasSuperseded()) return;
            }

            if (preflightWasSuperseded()) return;
            const dynamicTitle = generateConversationTitle(content);
            console.log(
              '[Chat] Creating new thread in database with title:',
              dynamicTitle
            );
            const newThread = await workspaceService.createThread(
              buildThreadCreateRequest({
                conversationId: threadConversation.id,
                title: dynamicTitle,
                projectId: boundProjectId,
              })
            );
            if (preflightWasSuperseded()) return;

            // Register in the chat store: the binding selectors and
            // setThreadProjectBinding read store.threads, and without this the
            // thread is invisible there until the next full loadThreads — a
            // project attached to it would be silently dropped from the UI.
            useChatStore.getState().registerThread(newThread);

            currentConversationId = newThread.id;
            currentThreadId = newThread.id;

            const newConv: ChatConversation = {
              id: newThread.id,
              title: newThread.title || dynamicTitle,
              messages: [],
              createdAt: Date.now(),
              updatedAt: Date.now(),
              threadId: newThread.id,
              conversationId: newThread.conversation_id,
              previewText: content,
              messageCount: 1,
            };

            setConversations((prev) => [newConv, ...prev]);
            useChatStore.getState().setCurrentThread(newConv.id);
            queueMicrotask(() => {
              if (
                preflightAbort.signal.aborted ||
                !ownsPreflight() ||
                useChatStore.getState().currentThreadId !== newThread.id
              ) {
                return;
              }
              if (navigateToThread) {
                navigateToThread(newThread.id);
              } else {
                router.replace(getSelectedThreadUrl(newThread.id));
              }
            });
            console.log('[Chat] Created new thread:', newThread.id);
          } catch (error) {
            if (preflightWasSuperseded()) return;
            console.error('[Chat] Failed to create thread:', error);
            // Roll back the optimistic turn: the user message was appended and
            // the composer cleared before this call. Without this the bubble
            // ghosts (never sent, gone on reload) and the typed text is lost.
            // Restore both and tell the user, so they can retry.
            rollbackPreflight();
            toast.error('Could not start the conversation. Please try again.');
            return;
          }
        }

        if (preflightWasSuperseded()) return;

        if (!currentThreadId) {
          rollbackPreflight();
          return;
        }

        const authRecoveryAttempt: StreamAuthRecoveryAttempt = {
          attemptId: crypto.randomUUID(),
          ownerUserId: ownerUserId ?? null,
          threadId: currentThreadId ?? '',
          promptStaged: false,
          authRefreshStarted: false,
          navigationStarted: false,
        };
        if (ownerUserId && currentThreadId) {
          authRecoveryAttempt.promptStaged = stageChatAuthRecovery({
            attemptId: authRecoveryAttempt.attemptId,
            ownerUserId,
            threadId: currentThreadId,
            prompt: content,
          });
        }
        authRecoveryAttemptRef.current = authRecoveryAttempt;

        // Stream via Agent (LangGraph) backend.
        // The workspace thread IS the agent thread (server-canonical).
        const existingAgentThreadId = currentThreadId || undefined;
        console.log(
          '[Chat] Starting agent stream, workspace thread:',
          currentThreadId,
          'agent thread:',
          existingAgentThreadId
        );

        if (currentThreadId) {
          useAgentActivityStore
            .getState()
            .startRun(currentThreadId, deriveAgentName(), deriveTask(content));
        }

        try {
          await runStreamTurn({
            currentThreadId,
            currentConversationId,
            newMessages,
            assistantRuntimeId,
            authRecoveryAttempt,
            start: (streamCallbacks, signal) =>
              agentChatService.streamMessage(
                {
                  messages: requestMessages
                    .filter((message) => message.source !== 'local-only')
                    .map((m, i, agentMessages) => ({
                      role: m.role,
                      content: m.content,
                      // Idempotency key rides on the user turn being sent (the
                      // last message) — server-canonical only.
                      ...(turnClientMessageId &&
                      i === agentMessages.length - 1 &&
                      m.role === 'user'
                        ? { client_message_id: turnClientMessageId }
                        : {}),
                    })),
                  page_context: {
                    type: boundProjectId ? 'project' : 'chat',
                    ...(activeWorkspaceId && {
                      workspace_id: activeWorkspaceId,
                    }),
                    ...(boundProjectId && {
                      project_id: boundProjectId,
                      project_name: resolvedProjectName || '',
                    }),
                    // The project is bound to the WORKSPACE thread, but this
                    // stream runs on a separate agent thread. The backend uses
                    // this id to resolve (and durably adopt) the bound project
                    // when the URL param has been dropped by thread navigation.
                    ...(currentThreadId && {
                      metadata: { workspace_thread_id: currentThreadId },
                    }),
                  },
                  use_rag: enableRAG,
                  thread_id: existingAgentThreadId,
                  // Documents the composer uploaded for this turn. The server
                  // links them to the user row it persists and drops any the
                  // caller's org does not own.
                  ...(attachmentIds && attachmentIds.length > 0
                    ? { attachment_ids: attachmentIds }
                    : {}),
                  // Edit-and-resend turns only: omitted entirely otherwise (and
                  // when the edited turn was never persisted with a cmid).
                  ...(supersedesClientMessageId
                    ? {
                        supersedes_client_message_id: supersedesClientMessageId,
                      }
                    : {}),
                },
                streamCallbacks,
                signal
              ),
          });
        } finally {
          submitLockRef.current = false;
        }
      } catch (err) {
        // A throw between lock-set and the inner try/finally used to leave
        // the lock stuck true (Send dead until remount). Reset and rethrow.
        submitLockRef.current = false;
        setIsLoading(false);
        throw err;
      } finally {
        preflightAbort.signal.removeEventListener('abort', rollbackPreflight);
        if (abortControllerRef.current === preflightAbort) {
          abortControllerRef.current = null;
        }
      }
    },
    [
      input,
      isLoading,
      storeIsStreaming,
      messages,
      displayedMessages,
      setMessages,
      dbConversation,
      workspace,
      setConversations,
      router,
      navigateToThread,
      enableRAG,
      boundProjectId,
      resolvedProjectName,
      runStreamTurn,
    ]
  );

  const handleStop = useCallback(() => {
    // Mark the stop first so the stream-completion path (which runs right after
    // the abort makes streamMessage resolve) keeps the partial answer and tags
    // it `stopped`, rather than wiping it here and racing the commit. The stop
    // targets whichever stream owns the slice right now.
    stopTargetRef.current = streamOwnerRef.current;
    // Snapshot the turn's citations BEFORE storeStopStreaming() wipes
    // streamingCitations — the completion path reads the store after the
    // wipe and would otherwise commit + persist a stopped RAG answer with
    // zero sources.
    stopCitationsRef.current = useChatStore.getState().streamingCitations;
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;

    // Close out the agent activity indicator — onDone won't fire on abort.
    // 'stopped', not 'done': a user abort must not strike through the
    // remaining plan items as if they completed.
    const runThread = activeRunThreadRef.current;
    if (runThread) {
      useAgentActivityStore.getState().finishRun(runThread, 'stopped');
    }

    // A parked confirmation has no live graph task for AbortController to
    // cancel. Close its durable run explicitly; if confirmation execution has
    // already started, aborting that live stream owns terminal cleanup.
    if (pendingConfirmation && !confirmLockRef.current) {
      void agentChatService
        .cancelPendingConfirmation(pendingConfirmation.threadId)
        .then(() =>
          setPendingConfirmation(null, pendingConfirmation.workspaceThreadId)
        )
        .catch((error) => {
          // Stop is locally authoritative: the user's intent stands even when
          // the durable cancel loses a race (409 once the graph resumed). The
          // per-turn stop target can't leak into later turns, so there is
          // nothing to un-set here — round-3 M2, resolved by design.
          console.error('[Chat] Failed to cancel pending confirmation:', error);
          toast.error('Could not stop this action. Please try again.');
        });
    } else {
      setPendingConfirmation(
        null,
        pendingConfirmation?.workspaceThreadId ?? null
      );
    }

    // Keep a parked confirmation visible until the durable cancellation wins.
    // A failed request remains retryable instead of reporting a false Stop.

    // The store-driven streaming path (used by the non-cloud chat) finalizes
    // through its own action; keep that contract intact.
    if (storeIsStreaming) {
      storeStopStreaming();
    }
  }, [
    pendingConfirmation,
    setPendingConfirmation,
    storeIsStreaming,
    storeStopStreaming,
  ]);

  // ---- Resume an in-flight stream on mount / thread switch ----
  // If the activity store still records a running run for the displayed
  // thread (e.g. the page remounted mid-stream), reattach to the backend's
  // buffered stream and replay from the last seen seq through the exact
  // same callbacks/commit path as a live submit.
  useEffect(() => {
    const threadId = activeThreadId;
    if (!threadId || isLoading) return;
    // A pending confirmation means the graph is deliberately parked waiting
    // for this user — not an interrupted stream to recover. Resuming here
    // fired on every HITL pause: it rendered a phantom "Thinking" bubble
    // under the card, its empty replay wiped the card, and its 204 closed the
    // activity rail while the graph was still interrupted.
    if (pendingConfirmation) return;
    if (useChatStore.getState().isStreaming) return;
    const run = useAgentActivityStore.getState().runs[threadId];
    const recoverableTransportFailure =
      run?.state === 'error' && transportFailureRef.current.has(threadId);
    if (!run || (run.state !== 'running' && !recoverableTransportFailure)) {
      return;
    }
    if (resumeTriedRef.current.has(threadId)) return;
    resumeTriedRef.current.add(threadId);
    transportFailureRef.current.delete(threadId);
    console.log('[Chat] Resuming in-flight agent stream:', threadId);
    // ponytail: messages is the snapshot at effect time — if thread history
    // is still loading, the resumed commit appends to a stale list; a
    // reload reconciles from the server-persisted rows.
    let replayGapSeen = false;
    void runStreamTurn({
      currentThreadId: threadId,
      currentConversationId: threadId,
      newMessages: messages,
      assistantRuntimeId: `resume:${threadId}:${run.startedAt}`,
      quietWhenEmpty: true,
      suppressCommit: () => replayGapSeen,
      keepRunOnFailure: true,
      start: async (streamCallbacks, signal) => {
        // The buffer trims old frames: if the replay starts past our cursor,
        // whatever we rebuild locally is missing a prefix of the answer. The
        // server rows are complete, so mark the thread stale and let the
        // reconcile that follows this turn replace the overlay wholesale.
        const gapAwareCallbacks: AgentStreamCallbacks = {
          ...streamCallbacks,
          onReplayGap: (firstSeq, expectedSeq) => {
            console.warn(
              `[Chat] Resume replay gap: buffer starts at ${firstSeq}, expected ${expectedSeq}`
            );
            replayGapSeen = true;
            useChatStore.getState().markMessagesStale(threadId);
          },
        };
        // A reattach can be killed by an idle proxy long before the run
        // itself is done (the resume path emits no heartbeats during silent
        // planner phases). One failure used to be terminal for the whole
        // activation; retry with backoff, always from the latest cursor so a
        // retry never replays what already committed.
        let lastError = 'Stream resume failed';
        for (let attempt = 0; attempt < RESUME_MAX_ATTEMPTS; attempt += 1) {
          if (signal.aborted) return;
          if (attempt > 0) {
            await new Promise((resolve) =>
              setTimeout(resolve, RESUME_BACKOFF_MS[attempt - 1])
            );
            if (signal.aborted) return;
          }
          const latestRun =
            useAgentActivityStore.getState().runs[threadId] ?? run;
          // onSeq defers its store write through a rAF, which a backgrounded
          // tab may never run — read the pending cursor too, or a retry
          // replays frames this turn already processed.
          const pendingSeq =
            pendingSeqRef.current?.threadId === threadId
              ? pendingSeqRef.current.seq
              : 0;
          const res = await agentChatService.resumeStream(
            threadId,
            Math.max(latestRun.streamSeq ?? 0, pendingSeq),
            gapAwareCallbacks,
            signal,
            latestRun.streamId
          );
          if (res.status === 'idle') {
            // Nothing active server-side — clear the stale run record.
            useAgentActivityStore.getState().finishRun(threadId, 'done');
            return;
          }
          if (res.status === 'resumed' || res.status === 'aborted') return;
          lastError = res.error ?? lastError;
          console.warn(
            `[Chat] Resume attempt ${attempt + 1} failed:`,
            lastError
          );
        }
        throw new Error(lastError);
      },
    });
    // storeIsStreaming is a dep so a thread with a stale run gets re-checked
    // once another thread's live stream ends (the guard above reads fresh).
  }, [
    activeThreadId,
    isLoading,
    messages,
    pendingConfirmation,
    runStreamTurn,
    storeIsStreaming,
  ]);

  // ---- Re-deliver a parked HITL confirmation on a cold thread load ----
  // The activity store is in-memory, so after a reload there is no run record
  // and the resume effect above can never fire. A thread parked on an
  // interrupt then rendered the user's message with no reply, no approval
  // card and an unlocked composer — so the user re-sent and parked a SECOND
  // interrupt. Ask the server once per thread activation: with no live stream
  // owning the thread, GET /stream/resume replays the pending confirmation as
  // a single frame (204 when there is none), which re-arms the gate the P4
  // effect renders and ChatSurface locks the composer on.
  useEffect(() => {
    const threadId = activeThreadId;
    if (!threadId || isLoading || storeIsStreaming) return;
    if (pendingConfirmation) return;
    if (useChatStore.getState().isStreaming) return;
    // A running run belongs to the resume effect; stopped/done means this
    // session already saw and settled the gate. An error run is different: an
    // auth-navigation abort can leave its confirmation parked server-side, so
    // allow the confirmation-only probe to check it once.
    const run = useAgentActivityStore.getState().runs[threadId];
    if (run && run.state !== 'error') return;
    if (confirmationProbedRef.current.has(threadId)) return;
    confirmationProbedRef.current.add(threadId);

    // Abort the previous thread's probe (if it is somehow still open) rather
    // than aborting in a cleanup: cleanup runs on every dep change and, with
    // the once-per-thread guard above, would cancel the probe without ever
    // re-issuing it (React StrictMode's double effect invocation does exactly
    // that).
    const probeAbort = new AbortController();
    probeAbortRef.current?.abort();
    probeAbortRef.current = probeAbort;
    // The probe is confirmation-only. If the replay opens with anything else,
    // this thread has a LIVE run server-side — keeping the probe attached
    // would consume that run's frames into the void, so bail immediately.
    const abandonProbe = (): void => probeAbort.abort();
    void Promise.resolve(
      agentChatService.resumeStream(
        threadId,
        0,
        {
          onConfirmation: (agentThreadId, confirmation) => {
            // A submit fired while the probe was in flight aborts it; a
            // confirmation frame that raced the abort must not arm a gate for
            // an interrupt the new turn just abandoned server-side.
            if (probeAbort.signal.aborted) return;
            setPendingConfirmation({
              threadId: agentThreadId,
              // Must be the DISPLAYED thread id, or confirmationBelongsToThread
              // rejects the card and Approve refuses to act.
              workspaceThreadId: threadId,
              approvalId: crypto.randomUUID(),
              confirmation,
              // No userRuntimeId/assistantRuntimeId here (unlike the live path):
              // the SSE confirmation frame carries only {thread_id, confirmation}
              // — the interrupted turn's ids live in the server-side run ledger,
              // which this probe never sees. Deriving them from the last
              // displayed user row is unsafe: legacy rows have no
              // client_message_id (their runtimeId is the persisted db id, not a
              // cmid), so a guessed identity could be WRONG, which mis-targets
              // reconciliation — absence degrades gracefully instead (the
              // confirm stream's done.client_message_id stays the primary key,
              // and confirmRuntimeId falls back to crypto.randomUUID()).
            });
            probeAbort.abort();
          },
          onToken: abandonProbe,
          onToolStart: abandonProbe,
          onStatus: abandonProbe,
          onPlan: abandonProbe,
        },
        probeAbort.signal
      )
    ).catch(() => {
      // Best-effort: a failed probe must not break the thread view.
    });
  }, [
    activeThreadId,
    isLoading,
    storeIsStreaming,
    pendingConfirmation,
    setPendingConfirmation,
  ]);

  // Unmount is the only place the in-flight probe is abandoned.
  useEffect(() => {
    return () => probeAbortRef.current?.abort();
  }, []);

  const handleConfirmation = useCallback(
    async (confirmed: boolean) => {
      const ownerUserId = useAuthStore.getState().user?.id;
      if (!pendingConfirmation) return;
      // CX1 belt: block a synchronous double-click before it can fire a
      // second streamConfirm call (see confirmLockRef declaration).
      if (confirmLockRef.current) return;
      // Defense in depth — the page hides the banner on foreign threads, but
      // a stale click must never resume a confirmation against the wrong
      // thread's transcript.
      if (
        !confirmationBelongsToThread(
          pendingConfirmation,
          useChatStore.getState().currentThreadId
        )
      )
        return;
      confirmLockRef.current = true;
      try {
        // The confirm resume is async; the user can still switch threads while it
        // streams. Gate every local setMessages below on the confirmation's
        // thread still being displayed — the resumed answer is persisted
        // server-side regardless, so a skipped local write is not lost (mirrors
        // handleSubmit's isTurnDisplayed guard).
        const isConfirmDisplayed = () =>
          confirmationBelongsToThread(
            pendingConfirmation,
            useChatStore.getState().currentThreadId
          );
        const confirmationThreadId =
          pendingConfirmation.workspaceThreadId || null;
        const confirmationDiagnostic = (terminalReason: string) => ({
          terminalReason,
          localCount: messages.length,
          completedInBackground: !isConfirmDisplayed(),
        });
        const reconcileConfirmationUser = (terminalReason: string) =>
          confirmationThreadId
            ? useChatStore.getState().refreshMessages(
                confirmationThreadId,
                pendingConfirmation.userRuntimeId
                  ? {
                      runtimeId: pendingConfirmation.userRuntimeId,
                      diagnostic: confirmationDiagnostic(terminalReason),
                    }
                  : undefined
              )
            : Promise.resolve(false);
        const reconcileConfirmationAssistant = (
          done: {
            assistant_message_id?: string | null;
            client_message_id?: string | null;
          },
          terminalReason: string
        ) =>
          confirmationThreadId
            ? useChatStore.getState().refreshMessages(confirmationThreadId, {
                persistedId: done.assistant_message_id ?? undefined,
                runtimeId:
                  done.client_message_id ??
                  pendingConfirmation.assistantRuntimeId,
                diagnostic: confirmationDiagnostic(terminalReason),
              })
            : Promise.resolve(false);
        if (confirmationThreadId) {
          useChatStore.getState().markMessagesStale(confirmationThreadId);
        }
        setIsConfirming(true);
        const confirmStart = Date.now();
        // Same stamp as the main stream: a resumed turn is its own turn, with
        // its own wait before the answer resumes. The backend already writes
        // ttft_ms for this path, so without this the split would appear only
        // after a reload.
        let confirmFirstTokenAt: number | null = null;
        // Track tool steps for the resumed turn exactly like handleSubmit —
        // seed with the pre-interrupt steps so the live bubble and the
        // committed message both show the whole turn's tools, not just the
        // post-confirm ones (previously none rendered until a page reload).
        const confirmSteps: ActivityStep[] = [
          ...(pendingConfirmation.steps ?? []),
        ];
        const confirmToolStartTimes = new Map<string, number[]>();
        // Pre-interrupt provenance carried on the confirmation — the interrupt
        // exit cleared the live streaming state, so restore it here.
        let confirmCitations = pendingConfirmation.citations ?? [];
        let confirmPlan: PlanStep[] = [...(pendingConfirmation.plan ?? [])];
        let confirmPlanReasoning = pendingConfirmation.planReasoning ?? '';
        let confirmProgress: AgentProgressStep[] = [
          ...(pendingConfirmation.progress ?? []),
        ];
        let confirmReasoning = '';
        // CX5: the confirm-resume path is a SEPARATE live-stream owner from
        // runStreamTurn (a resumed HITL turn belongs to the confirmation's
        // workspace thread, which may differ from whatever thread is
        // currently displayed) — stamp it the same way.
        const confirmStreamOwner = (streamOwnerRef.current += 1);
        const isConfirmStopped = (): boolean =>
          stopTargetRef.current === confirmStreamOwner;
        useChatStore.setState({
          isStreaming: true,
          streamingContent: '',
          streamingSteps: [...confirmSteps],
          // Seed from the pre-interrupt plan so it survives the HITL
          // resume — the interrupt exit already cleared streamingPlan.
          streamingPlan: [...confirmPlan],
          streamingProgress: [...confirmProgress],
          streamingReasoning: '',
          streamingCitations: confirmCitations,
          streamingElapsedMs: null,
          streamingPhase: 'accepted',
          streamingStatusDetail: null,
          isRetrievingRag: false,
          streamingThreadId: pendingConfirmation.workspaceThreadId || null,
        });

        let confirmContent = '';
        // `rag_context` events are cumulative snapshots for the interrupted
        // turn. `confirmCitations` starts with the carried pre-interrupt
        // snapshot and is replaced whenever the resume emits a newer one.
        // Token usage emitted by the confirm path (backend fires event: usage
        // before done). Without capturing this, confirmed turns showed no token
        // cost — inconsistent with the main stream.
        let confirmTokenUsage: { input: number; output: number } | null = null;
        // A resumed turn can hit ANOTHER destructive tool (nested interrupt):
        // the backend emits a fresh `confirmation` and ends the stream without
        // `done`. Captured here so `finally` re-arms the banner instead of
        // clearing it — previously the event was dropped and the graph was
        // left interrupted with no way to resume from the UI.
        let nestedConfirmation: PendingConfirmation | null = null;
        let confirmThrew = false;
        // Set when onDone/onError committed a bubble — the post-stream abort
        // path below must not double-commit.
        let confirmCommitted = false;
        let confirmHadError = false;
        let confirmDoneIds: {
          assistant_message_id?: string | null;
          client_message_id?: string | null;
          progress_steps?: AgentProgressStep[];
        } = {};
        const confirmMessages = [...messages];
        const confirmRuntimeId =
          pendingConfirmation.assistantRuntimeId ?? crypto.randomUUID();

        const buildConfirmMessage = (
          content: string,
          stopped: boolean
        ): ChatPageMessage => {
          return {
            runtimeId: confirmRuntimeId,
            source: 'optimistic',
            role: 'assistant',
            content,
            timestamp: Date.now(),
            ...(confirmCitations.length > 0
              ? { citations: confirmCitations.map(normalizeCitation) }
              : {}),
            ...(confirmSteps.length > 0
              ? { toolExecutions: [...confirmSteps] }
              : {}),
            ...(confirmPlan.length > 0 ? { plan: [...confirmPlan] } : {}),
            ...(confirmPlanReasoning
              ? { planReasoning: confirmPlanReasoning }
              : {}),
            ...(confirmProgress.length > 0
              ? { progressSteps: [...confirmProgress] }
              : {}),
            metadata: {
              responseTimeMs: Date.now() - confirmStart,
              ...(confirmFirstTokenAt !== null
                ? { ttftMs: confirmFirstTokenAt - confirmStart }
                : {}),
              ...(stopped ? { stopped: true } : {}),
              ...(confirmTokenUsage ? { tokenUsage: confirmTokenUsage } : {}),
              ...(confirmSteps.length > 0
                ? { toolsUsed: confirmSteps.map((s) => s.label) }
                : {}),
            },
          };
        };

        const confirmAbort = new AbortController();
        abortControllerRef.current = confirmAbort;
        const authRecoveryAttempt: StreamAuthRecoveryAttempt = {
          attemptId: crypto.randomUUID(),
          ownerUserId: ownerUserId ?? null,
          threadId: pendingConfirmation.workspaceThreadId,
          promptStaged: false,
          authRefreshStarted: false,
          navigationStarted: false,
        };
        authRecoveryAttemptRef.current = authRecoveryAttempt;

        try {
          await agentChatService.streamConfirm(
            { thread_id: pendingConfirmation.threadId, confirmed },
            {
              onToken: (content) => {
                if (confirmFirstTokenAt === null) {
                  confirmFirstTokenAt = Date.now();
                }
                confirmContent += content;
                pendingStreamContentRef.current = confirmContent;
                if (streamingRafRef.current === null) {
                  streamingRafRef.current = requestAnimationFrame(() => {
                    streamingRafRef.current = null;
                    if (pendingStreamContentRef.current !== null) {
                      useChatStore.setState({
                        streamingContent: pendingStreamContentRef.current,
                        isRetrievingRag: false,
                      });
                      pendingStreamContentRef.current = null;
                    }
                  });
                }
              },
              onReasoningDelta: (content) => {
                confirmReasoning = (confirmReasoning + content).slice(
                  0,
                  MAX_REASONING_SUMMARY_CHARS
                );
                useChatStore.setState({
                  streamingReasoning: confirmReasoning,
                });
              },
              onHeartbeat: (elapsedMs) => {
                useChatStore.setState({ streamingElapsedMs: elapsedMs });
              },
              onStatus: (phase, detail) => {
                confirmProgress = appendProgressStep(
                  confirmProgress,
                  phase,
                  detail
                );
                useChatStore.setState({
                  streamingPhase: phase,
                  streamingStatusDetail: detail ?? null,
                  streamingProgress: [...confirmProgress],
                });
              },
              // Same resume-cursor bookkeeping as the primary stream. Without
              // it the cursor froze at whatever seq the pre-interrupt turn
              // reached, so a disconnect mid-resume replayed the buffer from a
              // stale position instead of continuing after the last seen frame.
              onStreamId: (sid) => {
                const sidThreadId = pendingConfirmation.workspaceThreadId;
                if (!sidThreadId) return;
                streamIdByThreadRef.current[sidThreadId] = sid;
              },
              onSeq: (seq) => {
                const seqThreadId = pendingConfirmation.workspaceThreadId;
                if (!seqThreadId) return;
                pendingSeqRef.current = {
                  threadId: seqThreadId,
                  seq,
                  streamId: streamIdByThreadRef.current[seqThreadId],
                };
                if (seqRafRef.current === null) {
                  seqRafRef.current = requestAnimationFrame(() => {
                    seqRafRef.current = null;
                    const p = pendingSeqRef.current;
                    pendingSeqRef.current = null;
                    if (p) {
                      useAgentActivityStore
                        .getState()
                        .setStreamSeq(
                          p.threadId,
                          p.seq,
                          streamIdByThreadRef.current[p.threadId]
                        );
                    }
                  });
                }
              },
              onToolStart: (tool, args, callId) => {
                useAgentActivityStore
                  .getState()
                  .pushToolStart(
                    pendingConfirmation.workspaceThreadId,
                    tool,
                    callId
                  );
                const invocationKey = toolInvocationKey(tool, callId);
                confirmToolStartTimes.set(invocationKey, [
                  ...(confirmToolStartTimes.get(invocationKey) ?? []),
                  Date.now(),
                ]);
                confirmSteps.push({
                  ...(callId ? { id: callId } : {}),
                  tool,
                  label: toolLabel(tool),
                  status: 'running',
                  argsSummary: summarizeToolArgs(args),
                  ...(args && typeof args === 'object' ? { args } : {}),
                });
                useChatStore.setState({
                  streamingSteps: [...confirmSteps],
                  streamingStatusDetail: toolStatusLabel(tool, 'active'),
                });
              },
              onToolEnd: (tool, result, isError, callId) => {
                useAgentActivityStore
                  .getState()
                  .pushToolEnd(
                    pendingConfirmation.workspaceThreadId,
                    tool,
                    !isError,
                    callId
                  );
                // HITL-confirmed tools are exactly the mutating ones (ingest,
                // create_note, create_draft) — refresh the rail here too.
                invalidateProjectDataForTool(tool, isError);
                // create_project_note is destructive, so its successful
                // tool_end arrives HERE (post-approval resume stream), not on
                // the primary stream — auto-focus must run from this path.
                maybeAutoFocusCreatedNote(
                  tool,
                  result,
                  isError,
                  pendingConfirmation.workspaceThreadId
                );
                const invocationKey = toolInvocationKey(tool, callId);
                const startTime = confirmToolStartTimes
                  .get(invocationKey)
                  ?.pop();
                const durationMs = startTime
                  ? Date.now() - startTime
                  : undefined;
                const idx = findRunningToolStepIndex(
                  confirmSteps,
                  tool,
                  callId
                );
                if (idx !== undefined) {
                  confirmSteps[idx] = {
                    ...confirmSteps[idx],
                    status: isError ? 'error' : 'done',
                    durationMs,
                    resultSummary: summarizeToolResult(result),
                    result,
                  };
                }
                useChatStore.setState({
                  streamingSteps: [...confirmSteps],
                  streamingStatusDetail: toolStatusLabel(
                    tool,
                    isError ? 'error' : 'done'
                  ),
                });
              },
              onRagContext: (contexts) => {
                confirmCitations = contexts;
                useChatStore.setState({
                  streamingCitations: confirmCitations,
                  isRetrievingRag: false,
                  streamingStatusDetail: `Reading ${contexts.length} ${
                    contexts.length === 1 ? 'source' : 'sources'
                  }`,
                });
              },
              onPlan: (steps, reasoning) => {
                confirmPlan = toTurnPlan(steps);
                confirmPlanReasoning = reasoning;
                useChatStore.setState({
                  streamingPlan: [...confirmPlan],
                  streamingStatusDetail: 'Working through the plan',
                });
                const items = toActivityPlanItems(steps);
                if (pendingConfirmation.workspaceThreadId && items.length > 0) {
                  useAgentActivityStore
                    .getState()
                    .setPlan(pendingConfirmation.workspaceThreadId, items);
                }
              },
              onConfirmation: (threadId, confirmation) => {
                nestedConfirmation = {
                  threadId,
                  workspaceThreadId: pendingConfirmation.workspaceThreadId,
                  approvalId: crypto.randomUUID(),
                  confirmation,
                  steps: confirmSteps.filter((s) => s.status !== 'running'),
                  plan: [...confirmPlan],
                  planReasoning: confirmPlanReasoning || undefined,
                  citations: [...confirmCitations],
                  progress: [...confirmProgress],
                  userRuntimeId: pendingConfirmation.userRuntimeId,
                  assistantRuntimeId: pendingConfirmation.assistantRuntimeId,
                };
              },
              onUsage: (inputTokens, outputTokens) => {
                confirmTokenUsage = {
                  input: inputTokens,
                  output: outputTokens,
                };
              },
              onReflection: (_passed, _issues, _round, revising) => {
                if (!revising) return;
                confirmContent = '';
                pendingStreamContentRef.current = '';
                useChatStore.setState({ streamingContent: '' });
              },
              onAuthRefreshAttempt: () => {
                if (
                  authRecoveryAttempt &&
                  authRecoveryAttemptRef.current === authRecoveryAttempt
                ) {
                  authRecoveryAttempt.authRefreshStarted = true;
                }
              },
              onAuthRefreshSuccess: () => {
                if (authRecoveryAttemptRef.current === authRecoveryAttempt) {
                  authRecoveryAttempt.authRefreshStarted = false;
                }
              },
              onDone: (payload) => {
                finishAuthRecoveryAttempt(authRecoveryAttempt);
                confirmDoneIds = payload ?? {};
                if (confirmContent.trim()) {
                  const baseMessage = buildConfirmMessage(
                    confirmContent,
                    false
                  );
                  // The done payload carries the graph state's tool executions
                  // (parsed results, real durations) for the WHOLE turn —
                  // richer than the live SSE summaries, and identical to what
                  // a reload would show. Prefer them when present.
                  const serverSteps = mapDbToolExecutions(
                    payload?.tool_executions as DbToolExecution[] | undefined
                  );
                  const msg: ChatPageMessage = {
                    ...baseMessage,
                    ...(payload?.progress_steps?.length
                      ? { progressSteps: payload.progress_steps }
                      : {}),
                    ...(payload?.assistant_message_id
                      ? { id: payload.assistant_message_id }
                      : {}),
                    ...(serverSteps && serverSteps.length > 0
                      ? {
                          toolExecutions: serverSteps,
                          metadata: {
                            ...baseMessage.metadata,
                            toolsUsed: serverSteps.map((s) => s.label),
                          },
                        }
                      : {}),
                  };
                  confirmCommitted = true;
                  if (isConfirmDisplayed())
                    setMessages([...confirmMessages, msg]);
                }
              },
              onError: (error, category, localFailure) => {
                confirmHadError = true;
                if (
                  localFailure === 'authentication_required' &&
                  authRecoveryAttempt
                ) {
                  beginAuthRecovery(authRecoveryAttempt);
                  return;
                }
                finishAuthRecoveryAttempt(authRecoveryAttempt);
                // The confirm path used to commit a PLAIN content bubble for a
                // failure — no `error` block, so no Retry affordance and no
                // category. Same shape as the main stream now: server category
                // when the frame carried one, client fallback otherwise.
                const msg: ChatPageMessage = {
                  runtimeId: crypto.randomUUID(),
                  source: 'local-only',
                  role: 'assistant',
                  content: `Confirmation error: ${error}`,
                  timestamp: Date.now(),
                  error: {
                    message:
                      'This confirmation failed to complete. Please try again.',
                    category: localFailure ?? category ?? 'stream-error',
                  },
                };
                confirmCommitted = true;
                if (isConfirmDisplayed())
                  setMessages([...confirmMessages, msg]);
              },
            },
            confirmAbort.signal
          );

          // User hit Stop mid-resume: the abort swallows the stream so onDone
          // never fires — commit the partial answer tagged `stopped`, like
          // handleSubmit does. The backend's disconnect branch persists the
          // same partial server-side, so reload reconciles.
          if (
            !confirmCommitted &&
            isConfirmStopped() &&
            confirmContent.trim()
          ) {
            confirmCommitted = true;
            if (isConfirmDisplayed())
              setMessages([
                ...confirmMessages,
                buildConfirmMessage(confirmContent, true),
              ]);
          }
          if (
            nestedConfirmation ||
            confirmHadError ||
            authRecoveryAttempt.navigationStarted
          ) {
            await reconcileConfirmationUser(
              nestedConfirmation ? 'confirmation-paused' : 'confirmation-error'
            );
          } else {
            await reconcileConfirmationAssistant(
              confirmDoneIds,
              isConfirmStopped()
                ? 'confirmation-stopped'
                : confirmed
                  ? 'confirmation-approved'
                  : 'confirmation-rejected'
            );
          }
        } catch (err) {
          const errorMessage =
            err instanceof Error
              ? err.message
              : 'Network error during confirmation';
          const msg: ChatPageMessage = {
            runtimeId: crypto.randomUUID(),
            source: 'local-only',
            role: 'assistant',
            content: `Confirmation failed: ${errorMessage}`,
            timestamp: Date.now(),
            // Without the error block this bubble rendered as plain assistant
            // prose: no failure styling and no retry affordance, unlike every
            // other failed turn.
            error: {
              message: 'This action failed to complete. Please try again.',
              category: 'exception',
            },
          };
          if (isConfirmDisplayed()) setMessages([...confirmMessages, msg]);
          confirmThrew = true;
          await reconcileConfirmationUser('confirmation-exception');
        } finally {
          finishAuthRecoveryAttempt(authRecoveryAttempt);
          // Close out the agent activity rail — the interrupt left the run
          // "running" and neither onDone (confirm path) nor handleStop
          // (activeRunThreadRef is null here) ever finished it (round-3 M1).
          // A nested interrupt keeps the run open: the turn isn't over.
          // A failed attempt leaves the graph interrupted, so the run is not
          // over either — only finish it when the turn genuinely ended.
          const confirmFailed =
            confirmHadError ||
            confirmThrew ||
            authRecoveryAttempt.navigationStarted;
          if (
            !nestedConfirmation &&
            !confirmFailed &&
            pendingConfirmation.workspaceThreadId
          ) {
            useAgentActivityStore
              .getState()
              .finishRun(
                pendingConfirmation.workspaceThreadId,
                isConfirmStopped() ? 'stopped' : 'done'
              );
          }
          // A nested interrupt re-arms the banner with the new confirmation
          // (carrying the turn's accumulated provenance).
          //
          // On failure KEEP the current gate. Clearing it on a 500 or a dropped
          // connection stranded the backend: the graph stays interrupted, the
          // card disappears, and the only remaining move is to retype — which
          // discards the interrupt server-side ("Abandoned HITL interrupt
          // silently dropped"). Retaining it lets the user press Approve again,
          // and handleStop is the escape hatch if they'd rather abandon it.
          setPendingConfirmation(
            nestedConfirmation ??
              (confirmFailed
                ? {
                    ...pendingConfirmation,
                    approvalId: crypto.randomUUID(),
                    steps: confirmSteps.filter(
                      (step) => step.status !== 'running'
                    ),
                    plan: [...confirmPlan],
                    planReasoning: confirmPlanReasoning || undefined,
                    citations: [...confirmCitations],
                    progress: [...confirmProgress],
                  }
                : null),
            pendingConfirmation.workspaceThreadId
          );
          setIsConfirming(false);
          confirmLockRef.current = false;
          if (stopTargetRef.current === confirmStreamOwner) {
            stopTargetRef.current = null;
          }
          // The confirm stream shares streamingRafRef/pendingStreamContentRef
          // with handleSubmit's onToken throttle. A token that lands just
          // before completion schedules a rAF that would otherwise fire AFTER
          // this reset and resurrect stale streamingContent into the store.
          // Owner-guarded like runStreamTurn's teardown: a newer stream that
          // claimed the shared slot must not lose its first scheduled flush.
          if (streamOwnerRef.current === confirmStreamOwner) {
            if (streamingRafRef.current !== null) {
              cancelAnimationFrame(streamingRafRef.current);
              streamingRafRef.current = null;
            }
            flushPendingSeq();
            pendingStreamContentRef.current = null;
          }
          // Same ownership rule as runStreamTurn: a nested interrupt (or any
          // newer stream started while this one was reconciling) owns the
          // slice now, and must not be wiped by this turn's teardown.
          if (streamOwnerRef.current === confirmStreamOwner) {
            resetStreamingTurnState();
          }
        }
      } catch (err) {
        // A throw between lock-set and the try/finally used to leave the
        // lock stuck true (Approve/Deny dead until remount). Reset and
        // rethrow — mirrors the existing finally's confirmLockRef/isConfirming
        // resets so behavior for previously-observed errors is unchanged.
        confirmLockRef.current = false;
        setIsConfirming(false);
        throw err;
      }
    },
    [
      flushPendingSeq,
      beginAuthRecovery,
      finishAuthRecoveryAttempt,
      pendingConfirmation,
      messages,
      setMessages,
      invalidateProjectDataForTool,
      maybeAutoFocusCreatedNote,
      setPendingConfirmation,
    ]
  );

  // P4: mirror the active pending confirmation into an in-band approval
  // message the registered HitlApprovalToolUI renders in the transcript. Its
  // Approve/Deny call the runtime's respondToApproval, which routes through the
  // ExternalStore adapter (ChatRuntimeProvider's onRespondToToolApproval →
  // onApproval=handleConfirmation). handleConfirmation/streamConfirm internals
  // are untouched.
  useEffect(() => {
    if (!pendingConfirmation && !hadPendingApprovalRef.current) return;
    hadPendingApprovalRef.current = pendingConfirmation !== null;
    const active = confirmationBelongsToThread(
      pendingConfirmation,
      activeThreadId
    )
      ? pendingConfirmation
      : null;

    // Keep exactly one approval message in the transcript for the active gate.
    setMessages((prev) => {
      const withoutApproval = prev.filter((m) => !m.pendingApproval);
      if (!active) return withoutApproval;
      const preview = extractConfirmationPreview(active.confirmation);
      return [
        ...withoutApproval,
        {
          runtimeId: `approval:${active.workspaceThreadId}:${active.threadId}`,
          source: 'local-only',
          role: 'assistant' as const,
          content: '',
          timestamp: Date.now(),
          pendingApproval: { id: active.approvalId, tools: preview.tools },
        },
      ];
    });
  }, [pendingConfirmation, activeThreadId, setMessages]);

  return {
    input,
    setInput,
    isLoading,
    handleSubmit,
    handleStop,
    pendingConfirmation,
    isConfirming,
    handleConfirmation,
    chatInputRef,
    storeIsStreaming,
    storeStreamingContent,
    storeIsRetrievingRag,
    streamingThreadId,
    streamingTimestampRef,
  };
}
