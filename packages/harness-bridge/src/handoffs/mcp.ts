import { randomUUID } from "node:crypto";
import { ReauthenticationRequired, ToolRequestRejected } from "../mcp/client.ts";
import type { LocalTool } from "../mcp/server.ts";
import type { Handoff, HandoffCreate, HandoffHttpClient } from "./client.ts";

const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
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
 * `save_nous_handoff`: record a new handoff version. Identity and the chat come
 * from the grant; only the keys below are read from the model's arguments.
 */
export function saveHandoffTool(client: HandoffHttpClient): LocalTool {
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
      const handoffId = args.handoff_id ?? randomUUID();
      const parent = args.expected_parent_version;
      const { goal, decisions = [], remaining = [], results = [] } = args;
      if (!uuid(handoffId)) return { text: "handoff_id must be a UUID", isError: true };
      if (!(parent === null || (typeof parent === "number" && Number.isInteger(parent) && parent >= 1)))
        return { text: "expected_parent_version must be null or the latest version number", isError: true };
      if (typeof goal !== "string" || !goal.trim()) return { text: "save_nous_handoff requires a goal", isError: true };
      if (!lines(decisions) || !lines(remaining))
        return { text: "decisions and remaining must be lists of strings", isError: true };
      if (
        !Array.isArray(results) ||
        !results.every(
          (r) => typeof r === "object" && r !== null && uuid(r.artifact_version_id) && typeof r.summary === "string",
        )
      )
        return { text: "results must be a list of {artifact_version_id, summary}", isError: true };
      const name = args.harness_name ?? "codex";
      const session = args.harness_session_id;
      if (typeof name !== "string" || (session !== undefined && typeof session !== "string"))
        return { text: "harness_name and harness_session_id must be strings", isError: true };
      const payload: HandoffCreate = {
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
      try {
        const outcome = await client.save(payload);
        if ("saved" in outcome)
          return { text: `Saved handoff version ${outcome.saved.version}. ${JSON.stringify(outcome.saved)}`, structured: structured(outcome.saved) };
        // Not stored: the caller merges with the latest version and retries.
        if (outcome.conflict === null)
          return { text: `Not saved: ${outcome.detail ?? "NOUS reported a conflict"}.`, isError: true };
        return {
          text: `Not saved: the chat's latest handoff is version ${outcome.conflict.version}. Merge your changes into it and retry with expected_parent_version ${outcome.conflict.version} and a new handoff_id. Latest: ${JSON.stringify(outcome.conflict)}`,
          structured: structured(outcome.conflict),
          isError: true,
        };
      } catch (error) {
        if (error instanceof ToolRequestRejected || error instanceof ReauthenticationRequired) throw error;
        const reason = error instanceof Error ? error.message : String(error);
        console.error(`save_nous_handoff outcome unknown: ${reason}`);
        return {
          text: `save outcome unknown: ${reason}. Retry with the same handoff_id ${handoffId}; NOUS returns the stored version.`,
          isError: true,
        };
      }
    },
  };
}
