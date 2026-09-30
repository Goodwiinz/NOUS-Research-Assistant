/**
 * Agent SSE event vocabulary — the single frontend mirror of the backend
 * `AgentStreamEvent` StrEnum (backend/src/shared/enums.py).
 *
 * These values ARE the SSE wire contract: they must stay byte-identical to the
 * backend enum's values. This is the ONE place the frontend enumerates the
 * event names — the consumer switch in `agentChatService.ts` dispatches on
 * them, and `agentStreamEvents.contract.test.ts` fails CI if the switch (or its
 * exported `HANDLED_STREAM_EVENTS` set) drifts from this list. Keep this list in
 * lockstep with the backend enum by hand; a mismatch surfaces as a dropped or
 * unhandled event, so both the backend and frontend contract tests guard it.
 */
export const AGENT_STREAM_EVENTS = [
  'token',
  'reasoning_delta',
  'tool_start',
  'tool_end',
  'rag_context',
  'plan',
  'reflection',
  'trace',
  'usage',
  'heartbeat',
  'status',
  'confirmation',
  'approval_required',
  'done',
  'error',
  'artifact',
] as const;

/** Union of every agent SSE event name (mirror of backend `AgentStreamEvent`). */
export type AgentStreamEvent = (typeof AGENT_STREAM_EVENTS)[number];

/**
 * Server-authored error categories — the frontend mirror of the backend
 * `AgentErrorCategory` StrEnum (backend/src/shared/enums.py).
 *
 * An `error` frame is flat: `{ error: "<message string>", category: "<label>" }`.
 * `error` stays a STRING (wire compat — it was never an object); `category` is
 * a sibling key the backend authors at the emit site, so the UI can branch on
 * *why* a turn failed without regex-matching prose.
 *
 * Unknown values are ignored rather than trusted: an older frontend talking to
 * a newer backend must degrade to the generic error treatment, not render a
 * label it does not understand. `parseAgentErrorCategory` is that gate.
 */
export const AGENT_ERROR_CATEGORIES = [
  'upstream_timeout',
  'model_error',
  'tool_error',
  'checkpoint_unavailable',
  'rate_limited',
  'cancelled',
  'invalid_request',
  'conflict',
  'internal',
] as const;

/** Union of every server-authored error category (mirror of backend enum). */
export type AgentErrorCategory = (typeof AGENT_ERROR_CATEGORIES)[number];

const AGENT_ERROR_CATEGORY_SET: ReadonlySet<string> = new Set(
  AGENT_ERROR_CATEGORIES
);

/**
 * Validate an untrusted `category` value off the wire. Returns the category
 * when it is one this build knows, `undefined` otherwise (unknown label, wrong
 * type, or absent — all of which mean "no server claim about the cause").
 */
export function parseAgentErrorCategory(
  value: unknown
): AgentErrorCategory | undefined {
  return typeof value === 'string' && AGENT_ERROR_CATEGORY_SET.has(value)
    ? (value as AgentErrorCategory)
    : undefined;
}

export const AGENT_STREAM_PHASES = [
  'accepted',
  'routing',
  'retrieving',
  'planning',
  'writing',
  'finalizing',
] as const;

export type AgentStreamPhase = (typeof AGENT_STREAM_PHASES)[number];

/** Display-safe progress persisted with an assistant turn. */
export interface AgentProgressStep {
  phase: AgentStreamPhase;
  detail: string;
}

/**
 * `heartbeat` is the keepalive the backend emits during silent planner/LLM
 * phases. It carries `elapsed_ms`, which the consumer surfaces as live
 * progress on the thinking pill — like every other event, it is handled.
 */
export const HEARTBEAT_STREAM_EVENT = 'heartbeat' satisfies AgentStreamEvent;

/**
 * Terminal frames — the live stream (and the resumable replay) ends after one
 * of these. Mirror of backend `TERMINAL_STREAM_EVENTS`.
 */
export const TERMINAL_STREAM_EVENTS: ReadonlySet<AgentStreamEvent> = new Set([
  'done',
  'error',
  'confirmation',
]);
