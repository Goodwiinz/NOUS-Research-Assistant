import {
  MessageRole,
  type ChatMessage,
  type DbToolExecution,
  type MessageAttachment,
} from '@/types/workspace';
import { normalizeCitation } from '@/utils/citationNormalizer';
import type { Citation } from '@/utils/citationParser';
import type { PlanStep } from '@/types/agent-chat';
import type { AgentProgressStep } from '@/services/agentStreamEvents';
import { toolLabel } from '@/components/context-rail/toolLabels';

import type { ActivityStep } from '@nous/chat-runtime/types';
export type { ActivityStep } from '@nous/chat-runtime/types';

const SUMMARY_MAX = 140;

function truncate(s: string): string {
  const oneLine = s.replace(/\s+/g, ' ').trim();
  return oneLine.length > SUMMARY_MAX
    ? oneLine.slice(0, SUMMARY_MAX - 1) + '…'
    : oneLine;
}

/** One-line `key: value` summary of tool-call args for the activity strip. */
export function summarizeToolArgs(
  args: Record<string, unknown> | undefined
): string | undefined {
  if (!args || typeof args !== 'object') return undefined;
  const entries = Object.entries(args).filter(([, v]) => v != null);
  if (entries.length === 0) return undefined;
  const joined = entries
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' · ');
  return truncate(joined);
}

/**
 * One-line summary of a tool result for the activity strip. Live-stream
 * results arrive as JSON strings, but the backend PERSISTS them as parsed
 * objects — so accept both: prefer a `message`/`error`/`summary` field, fall
 * back to the truncated raw text (stringifying objects).
 */
export function summarizeToolResult(result: unknown): string | undefined {
  if (result == null || result === '') return undefined;
  let raw: string;
  try {
    raw = typeof result === 'string' ? result : JSON.stringify(result);
  } catch {
    return truncate(String(result));
  }
  if (!raw) return undefined;
  try {
    const parsed: unknown =
      typeof result === 'string' ? JSON.parse(raw) : result;
    if (parsed && typeof parsed === 'object') {
      const obj = parsed as Record<string, unknown>;
      const msg = obj.error ?? obj.message ?? obj.summary;
      if (typeof msg === 'string' && msg.length > 0) return truncate(msg);
    }
  } catch {
    // not JSON — fall through to raw text
  }
  return truncate(raw);
}

/**
 * Map persisted chat_messages.tool_executions rows onto the activity-strip
 * shape so a reloaded thread shows the same tool steps as the live turn.
 * Persisted rows are always settled: anything not failed reads as done.
 */
export function mapDbToolExecutions(
  execs: DbToolExecution[] | undefined
): ActivityStep[] | undefined {
  if (!execs || execs.length === 0) return undefined;
  return execs.map((e) => {
    const argsSummary = summarizeToolArgs(e.args);
    const resultSummary = e.error
      ? summarizeToolResult(e.error)
      : summarizeToolResult(e.result);
    return {
      ...(e.id ? { id: e.id } : {}),
      tool: e.tool_name,
      label: toolLabel(e.tool_name, e.tool_display_name),
      status:
        e.status === 'failed' || e.error
          ? 'error'
          : e.status === 'cancelled' || e.status === 'running'
            ? 'cancelled'
            : 'done',
      ...(typeof e.duration_ms === 'number'
        ? { durationMs: e.duration_ms }
        : {}),
      ...(argsSummary ? { argsSummary } : {}),
      ...(e.args && typeof e.args === 'object' ? { args: e.args } : {}),
      ...(resultSummary ? { resultSummary } : {}),
      ...(e.result !== undefined ? { result: e.result } : {}),
    } satisfies ActivityStep;
  });
}

export interface ChatPageMessage {
  /** Stable identity for React/assistant-ui across optimistic persistence.
   * Unlike `id`, this never changes when the server row arrives. */
  runtimeId: string;
  /** Provenance controls canonical reconciliation and history inclusion. */
  source: 'canonical' | 'optimistic' | 'local-only';
  /** Persisted database identity. Only use this for database operations. */
  id?: string;
  /** The turn's server-side idempotency key (`chat_messages.client_message_id`),
   * when it has one. Distinct from `runtimeId`, which falls back to the row id
   * for legacy rows — edit-and-resend needs the unambiguous value because the
   * backend looks the superseded turn up BY client_message_id. */
  clientMessageId?: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  citations?: Citation[];
  diagnosticsTraceId?: string;
  attachments?: MessageAttachment[];
  /** Tool executions recorded during the turn that produced this message. */
  toolExecutions?: ActivityStep[];
  /** Structured execution plan emitted by the agent planner for this turn. */
  plan?: PlanStep[];
  /** Planner's top-level rationale for `plan`. */
  planReasoning?: string;
  /** Display-safe progress persisted with this assistant turn. */
  progressSteps?: AgentProgressStep[];
  /** Transient marker on the in-flight assistant turn path: the
   * message is a live placeholder whose text/steps/citations are read from the
   * streaming store, not from these fields. Cleared when the turn commits. */
  isStreaming?: boolean;
  /** In-band HITL approval gate (P4): the agent paused awaiting
   * confirmation of this tool. convertMessage emits an approval tool-call part
   * that the registered HitlApprovalToolUI renders in the message stream. */
  pendingApproval?: {
    id: string;
    tools: Array<{ name: string; args: Record<string, unknown> }>;
  };
  metadata?: {
    toolsUsed?: string[];
    responseTimeMs?: number;
    /** Time to first token, same origin as `responseTimeMs` — the difference
     * is the time spent writing the answer. Absent when the turn streamed no
     * token, and on rows persisted before the column existed. */
    ttftMs?: number;
    sourcesCount?: number;
    /** The user stopped this response mid-stream; the text is partial. */
    stopped?: boolean;
    /** Per-turn LLM token usage (persisted in chat_messages.token_usage). */
    tokenUsage?: { input: number; output: number };
  };
  /** Recoverable-failure marker on an assistant turn (network error, empty
   * response, stream exception). Renders an error block with a Retry button
   * instead of an ambiguous blank bubble. Local-only; never persisted. */
  error?: { message: string; category?: string };
  /** Persisted per-response feedback (chat_messages.feedback_rating /
   * feedback_text). Present on canonical rows the user has rated. */
  feedback?: { rating: number | null; comment: string | null };
}

/**
 * Canonical single-message mapper from a persisted `ChatMessage` row to the
 * UI `ChatPageMessage` shape. Every code path that turns a DB/server message
 * into something the chat list renders MUST go through this — there used to
 * be two parallel mappers (`mapStoreMessagesToChatMessages` here and
 * `mapDbMessageToUiMessage` in `useChatSession`) that drifted, and the
 * lazy-load path silently dropped `plan` + `token_usage` because only this
 * one carried them.
 */
export function mapDbMessageToChatPageMessage(
  dbMsg: ChatMessage
): ChatPageMessage {
  const hasMetadata =
    dbMsg.latency_ms || dbMsg.ttft_ms || dbMsg.stopped || dbMsg.token_usage;
  const hasFeedback = dbMsg.feedback_rating != null || !!dbMsg.feedback_text;
  return {
    id: dbMsg.id,
    runtimeId: dbMsg.client_message_id ?? dbMsg.id,
    ...(dbMsg.client_message_id
      ? { clientMessageId: dbMsg.client_message_id }
      : {}),
    source: 'canonical',
    role:
      dbMsg.role === MessageRole.USER
        ? ('user' as const)
        : ('assistant' as const),
    content: dbMsg.content,
    timestamp: new Date(dbMsg.created_at).getTime(),
    citations: dbMsg.citations?.map(normalizeCitation),
    attachments: dbMsg.attachments,
    toolExecutions: mapDbToolExecutions(dbMsg.tool_executions),
    ...(dbMsg.plan && dbMsg.plan.length > 0 ? { plan: dbMsg.plan } : {}),
    ...(dbMsg.plan_reasoning ? { planReasoning: dbMsg.plan_reasoning } : {}),
    ...(dbMsg.progress_steps && dbMsg.progress_steps.length > 0
      ? { progressSteps: dbMsg.progress_steps }
      : {}),
    ...(hasFeedback
      ? {
          feedback: {
            rating: dbMsg.feedback_rating ?? null,
            comment: dbMsg.feedback_text ?? null,
          },
        }
      : {}),
    metadata: hasMetadata
      ? {
          ...(dbMsg.latency_ms ? { responseTimeMs: dbMsg.latency_ms } : {}),
          ...(dbMsg.ttft_ms ? { ttftMs: dbMsg.ttft_ms } : {}),
          ...(dbMsg.stopped ? { stopped: true } : {}),
          ...(dbMsg.token_usage
            ? {
                tokenUsage: {
                  input: dbMsg.token_usage.input_tokens,
                  output: dbMsg.token_usage.output_tokens,
                },
              }
            : {}),
        }
      : undefined,
  };
}

export function mapStoreMessagesToChatMessages(
  messages: ChatMessage[]
): ChatPageMessage[] {
  return messages.map(mapDbMessageToChatPageMessage);
}

interface SelectDisplayedMessagesParams {
  localMessages: ChatPageMessage[];
  storeMessages?: ChatMessage[];
  messageFreshness?: 'fresh' | 'stale' | 'refreshing';
}

/**
 * True only while the ACTIVE thread's initial page is in flight and there is
 * nothing renderable for it yet — the sole case that warrants a transcript
 * skeleton. Local optimistic/streaming turns (first send in a new chat) and
 * already-cached store pages keep rendering; background refreshes never blank
 * the transcript. Scoping the gate this way is what fixes the #1121
 * regressions (blanked first send, skeleton flash on cached thread switches).
 */
export function isThreadSwitchPending(params: {
  activeThreadId: string | null;
  loadingThreadId: string | null;
  localMessageCount: number;
  storeMessageCount: number;
}): boolean {
  return (
    params.activeThreadId !== null &&
    params.loadingThreadId === params.activeThreadId &&
    params.localMessageCount === 0 &&
    params.storeMessageCount === 0
  );
}

/**
 * Carry in-memory turn provenance over to the server-mapped message.
 * plan / token_usage are persisted now, but the store page may have been
 * fetched BEFORE the assistant row landed (or the row predates the columns),
 * so when the displayed list flips from local to store messages after
 * `done`, the plan and token badge would silently vanish from the
 * just-finished turn. Server values stay canonical where both exist.
 */
function mergeLocalProvenance(
  mapped: ChatPageMessage[],
  localMessages: ChatPageMessage[]
): ChatPageMessage[] {
  if (localMessages.length === 0) return mapped;

  const localByRuntimeId = new Map(
    localMessages
      .filter((message) => !!message.runtimeId)
      .map((message) => [message.runtimeId, message])
  );

  return mapped.map((message) => {
    const local = localByRuntimeId.get(message.runtimeId);
    if (!local) return message;

    const mergedMetadata =
      local.metadata || message.metadata
        ? { ...local.metadata, ...message.metadata }
        : undefined;

    // Legacy (client-persist) rows have no tool_executions column data, so
    // the flip from local to store messages dropped the tool-activity strip
    // from the just-finished turn (round-3 M3). Server values stay canonical
    // where both exist.
    const mergedToolExecutions = message.toolExecutions?.length
      ? message.toolExecutions
      : local.toolExecutions;

    return {
      ...message,
      ...((message.plan ?? local.plan)
        ? { plan: message.plan ?? local.plan }
        : {}),
      ...((message.planReasoning ?? local.planReasoning)
        ? { planReasoning: message.planReasoning ?? local.planReasoning }
        : {}),
      ...(mergedToolExecutions?.length
        ? { toolExecutions: mergedToolExecutions }
        : {}),
      ...(mergedMetadata ? { metadata: mergedMetadata } : {}),
    };
  });
}

export function selectDisplayedMessages({
  localMessages,
  storeMessages = [],
  messageFreshness,
}: SelectDisplayedMessagesParams): ChatPageMessage[] {
  const canonical = mergeLocalProvenance(
    mapStoreMessagesToChatMessages(storeMessages),
    localMessages
  );
  const canonicalRuntimeIds = new Set(
    canonical.map((message) => message.runtimeId)
  );

  // A known-empty fresh page is authoritative. During initial/stale loads,
  // however, the local projection is the only renderable transcript and must
  // not disappear while the request is in flight.
  if (canonical.length === 0 && messageFreshness !== 'fresh') {
    return localMessages;
  }

  const overlays = localMessages.filter((message) => {
    if (canonicalRuntimeIds.has(message.runtimeId)) return false;
    if (message.source === 'local-only') return true;
    return message.source === 'optimistic' && messageFreshness !== 'fresh';
  });

  return [...canonical, ...overlays];
}
