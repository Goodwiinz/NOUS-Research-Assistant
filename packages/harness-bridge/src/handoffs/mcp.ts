import { randomUUID } from "node:crypto";
import { ReauthenticationRequired, ToolRequestRejected } from "../mcp/client.ts";
import type { LocalTool } from "../mcp/server.ts";
import { uuid, type Handoff, type HandoffCreate, type HandoffHttpClient } from "./client.ts";
import type { Binding, HandoffQueue } from "./queue.ts";
const lines = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === "string");

const LINES = { type: "array", maxItems: 50, items: { type: "string", minLength: 1, maxLength: 500 } };

function structured(handoff: Handoff): Record<string, unknown> {
  return handoff as unknown as Record<string, unknown>;
}

/** `get_nous_handoff`: read the latest handoff left in the bound chat. */
export function getHandoffTool(client: HandoffHttpClient): LocalTool {
  return {
    descriptor: {
      name: "get_nous_handoff",
      description:
        "Read the latest structured handoff (goal, decisions, results, remaining work) left in the NOUS chat this session is bound to. Continue from it; pass its version as expected_parent_version when saving.",
      inputSchema: { type: "object", additionalProperties: false, properties: {} },
    },
    async call() {
      const latest = await client.getLatest();
      if (latest === null)
        return { text: "No handoff exists in this chat yet. Save the first one with expected_parent_version null." };
      return { text: JSON.stringify(latest), structured: structured(latest) };
    },
  };
}

/**
 * Validate handoff fields from model arguments or a CLI file; returns an
 * error message when invalid. Only the keys below are read.
 */
export function handoffPayload(args: Record<string, any>, defaultHarness: string): HandoffCreate | string {
  const handoffId = args.handoff_id ?? randomUUID();
  const parent = args.expected_parent_version;
  const { goal, decisions = [], remaining = [], results = [] } = args;
  if (!uuid(handoffId)) return "handoff_id must be a UUID";
  if (!(parent === null || (typeof parent === "number" && Number.isInteger(parent) && parent >= 1)))
    return "expected_parent_version must be null or the latest version number";
  if (typeof goal !== "string" || !goal.trim()) return "save_nous_handoff requires a goal";
  if (!lines(decisions) || !lines(remaining)) return "decisions and remaining must be lists of strings";
  if (
    !Array.isArray(results) ||
    !results.every(
      (r) => typeof r === "object" && r !== null && uuid(r.artifact_version_id) && typeof r.summary === "string",
    )
  )
    return "results must be a list of {artifact_version_id, summary}";
  const name = args.harness_name ?? defaultHarness;
  const session = args.harness_session_id;
  if (typeof name !== "string" || (session !== undefined && typeof session !== "string"))
    return "harness_name and harness_session_id must be strings";
  return {
    handoff_id: handoffId,
    expected_parent_version: parent,
    goal,
    decisions,
    remaining,
    results: results.map((r: { artifact_version_id: string; summary: string }) => ({
      artifact_version_id: r.artifact_version_id,
      summary: r.summary,
    })),
    harness_name: name,
    ...(session !== undefined ? { harness_session_id: session } : {}),
  };
}

/**
 * `save_nous_handoff`: record a new handoff version. Identity and the chat come
 * from the grant; only the keys below are read from the model's arguments.
 * Every save goes through the local queue (journal first, then POST).
 */
export function saveHandoffTool(queue: HandoffQueue, binding: () => Promise<Binding | null>): LocalTool {
  return {
    descriptor: {
      name: "save_nous_handoff",
      description:
        "Save a structured handoff to the bound NOUS chat so a later session can continue. Read the latest with get_nous_handoff first and pass its version as expected_parent_version (null only for the first). On a conflict, merge with the returned latest version and retry. Reuse handoff_id to retry the same save safely.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["expected_parent_version", "goal"],
        properties: {
          handoff_id: { type: "string", format: "uuid", description: "Client-generated UUID; generated when omitted. Identical retries return the stored version" },
          expected_parent_version: { type: ["integer", "null"], minimum: 1 },
          goal: { type: "string", minLength: 1, maxLength: 2000 },
          decisions: LINES,
          remaining: LINES,
          results: {
            type: "array",
            maxItems: 50,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["artifact_version_id", "summary"],
              properties: {
                artifact_version_id: { type: "string", format: "uuid", description: "A version published to this NOUS project" },
                summary: { type: "string", minLength: 1, maxLength: 500 },
              },
            },
          },
          harness_name: { type: "string", minLength: 1, maxLength: 64, description: "Defaults to codex" },
          harness_session_id: { type: "string", minLength: 1, maxLength: 128 },
        },
      },
    },
    async call(args) {
      const payload = handoffPayload(args, "codex");
      if (typeof payload === "string") return { text: payload, isError: true };
      const bound = await binding();
      if (bound === null)
        return {
          text: "This MCP session's credential is not the device's current chat binding (the device was reconnected or is not bound to a chat); nothing was saved. Reconnect this MCP session: restart it after nous-harness connect --chat UUID --tools --handoff.",
          isError: true,
        };
      // Journaled locally first; an unconfirmed save survives for `handoff flush`.
      const attempt = await queue.submit(bound, payload);
      if (attempt.state === "done")
        return { text: `Saved handoff version ${attempt.saved.version}. ${JSON.stringify(attempt.saved)}`, structured: structured(attempt.saved) };
      if (attempt.state === "conflicted") {
        // Not stored: the caller merges with the latest version and retries.
        if (attempt.latest === null)
          return { text: `Not saved: ${attempt.detail}. Read the latest with get_nous_handoff, merge, and retry.`, isError: true };
        return {
          text: `Not saved: the chat's latest handoff is version ${attempt.latest.version}. Merge your changes into it and retry with expected_parent_version ${attempt.latest.version} and a new handoff_id. Latest: ${JSON.stringify(attempt.latest)}`,
          structured: structured(attempt.latest),
          isError: true,
        };
      }
      const reason = attempt.error.message;
      if (attempt.state === "rejected") {
        if (attempt.error instanceof ToolRequestRejected || attempt.error instanceof ReauthenticationRequired)
          throw attempt.error;
        return { text: `Not saved: ${reason}`, isError: true };
      }
      // Pending (network, 5xx, 429, 401, expired grant): say it is journaled, never just throw.
      console.error(`save_nous_handoff outcome unknown: ${reason}`);
      return {
        text:
          `Save not confirmed: ${reason}. It is queued locally as pending under handoff_id ${payload.handoff_id}; retry with that same handoff_id (NOUS returns the stored version) or run nous-harness handoff flush.` +
          (attempt.reconnect ? " Reconnect first: nous-harness connect --chat UUID --tools --handoff." : ""),
        isError: true,
      };
    },
  };
}
