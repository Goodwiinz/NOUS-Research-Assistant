import { ReauthenticationRequired, ToolRequestRejected } from "../mcp/client.ts";
import type { LocalTool } from "../mcp/server.ts";
import type { ActionHttpClient, ActionStatus } from "./client.ts";

const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

/**
 * Plain-language outcome for the model. Only `succeeded` says the note
 * exists; every other state says what the user or NOUS still has to do.
 */
export function describe(status: ActionStatus): string {
  const id = status.invocation_id;
  switch (status.state) {
    case "awaiting_approval":
      return `Not created yet. The user must approve this note in NOUS: ${status.approval_url ?? "open NOUS and review the pending action"}. Check later with get_action_status (invocation_id ${id}); do not request it again.`;
    case "approved":
    case "executing":
      return `Approved; NOUS is creating the note. Check again with get_action_status (invocation_id ${id}).`;
    case "succeeded":
      return `Created. ${JSON.stringify(status.result?.content?.[0] ?? {})}`;
    case "failed":
      return `Not created: ${JSON.stringify(status.result?.content?.[0] ?? "the user denied it or NOUS refused it")}.`;
    case "outcome_unknown":
      return `Outcome unknown: NOUS could not confirm whether the note was created. Do not retry; ask the user to check the project notes (invocation_id ${id}).`;
  }
}

function result(status: ActionStatus): { text: string; structured: Record<string, unknown>; isError?: boolean } {
  return {
    text: describe(status),
    structured: status as unknown as Record<string, unknown>,
    ...(status.state === "failed" || status.state === "outcome_unknown" ? { isError: true } : {}),
  };
}

/** `request_action`: ask the user to approve a note. Never runs anything by itself. */
export function requestActionTool(client: ActionHttpClient): LocalTool {
  return {
    descriptor: {
      name: "request_action",
      description:
        "Ask the user to approve creating a note in the granted NOUS project. Returns an approval link; the note is only created after the user approves in NOUS. Reuse the same invocation_id to retry safely.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["action", "invocation_id", "title", "content"],
        properties: {
          action: { type: "string", enum: ["create_project_note"] },
          invocation_id: { type: "string", format: "uuid", description: "Client-generated UUID; identical retries return the same request" },
          title: { type: "string", maxLength: 255 },
          content: { type: "string", maxLength: 200000, description: "Markdown body" },
          tags: { type: "array", items: { type: "string", minLength: 1, maxLength: 64 } },
        },
      },
    },
    async call(args) {
      const { action, invocation_id: invocationId, title, content, tags } = args;
      if (action !== "create_project_note")
        return { text: "request_action only supports action=create_project_note", isError: true };
      if (!uuid(invocationId) || typeof title !== "string" || !title.trim() || typeof content !== "string" || !content.trim())
        return { text: "request_action requires a UUID invocation_id, a title and content", isError: true };
      if (tags !== undefined && !(Array.isArray(tags) && tags.every((t) => typeof t === "string")))
        return { text: "tags must be a list of strings", isError: true };
      try {
        return result(
          await client.requestNote({
            invocationId,
            title,
            content,
            ...(Array.isArray(tags) ? { tags: tags as string[] } : {}),
          }),
        );
      } catch (error) {
        if (error instanceof ToolRequestRejected || error instanceof ReauthenticationRequired) throw error;
        const reason = error instanceof Error ? error.message : String(error);
        console.error(`request_action outcome unknown: ${reason}`);
        return {
          text: `request outcome unknown: ${reason}. Retry with the same invocation_id ${invocationId}; NOUS returns the stored request.`,
          isError: true,
        };
      }
    },
  };
}

/** `get_action_status`: read the stored outcome of a requested action. */
export function actionStatusTool(client: ActionHttpClient): LocalTool {
  return {
    descriptor: {
      name: "get_action_status",
      description: "Read whether a requested NOUS action is still waiting for approval, was created, failed, or has an unknown outcome.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["invocation_id"],
        properties: { invocation_id: { type: "string", format: "uuid" } },
      },
    },
    async call(args) {
      if (!uuid(args.invocation_id)) return { text: "get_action_status requires a UUID invocation_id", isError: true };
      try {
        return result(await client.status(args.invocation_id));
      } catch (error) {
        // Actions are scoped to the connection's consent; after a reconnect an
        // earlier request is not visible, which does not mean it never existed.
        if (error instanceof ToolRequestRejected && /not found/i.test(error.message))
          return {
            text: `Action ${args.invocation_id} is not visible to this connection; it may belong to an earlier connection. Do not request it again; ask the user to check NOUS.`,
            isError: true,
          };
        throw error;
      }
    },
  };
}
