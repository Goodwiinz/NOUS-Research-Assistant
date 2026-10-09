import type { components } from "../../../../frontend/src/types/generated/api.d.ts";
import { integrationHeaders, type IntegrationCredentials } from "../credentials.ts";
import { apiBase, throwForStatus } from "../mcp/client.ts";

export type ActionStatus = components["schemas"]["ActionStatus"];
type ToolInvocation = components["schemas"]["ToolInvocation"];
/** A `create_project_note` request; see `ActionHttpClient.requestNote`. */
export type ActionRequest = {
  invocationId: string;
  title: string;
  content: string;
  tags?: string[];
};

const MESSAGES = {
  forbidden:
    "NOUS denied this action: the grant may lack the scope it needs (tools:write; library:write covers only the reversible library changes; reconnect with nous-harness connect --tools --write --library), or a project or document it names is outside the grant (a project connection reaches only its own project, so it cannot create a folder or move papers between two)",
  disabled: "NOUS integration actions are disabled",
};
const STATES = new Set([
  "awaiting_approval",
  "approved",
  "executing",
  "succeeded",
  "failed",
  "outcome_unknown",
]);
const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

function parseStatus(data: unknown): ActionStatus {
  if (
    typeof data !== "object" ||
    data === null ||
    !uuid((data as ActionStatus).invocation_id) ||
    !STATES.has((data as ActionStatus).state)
  )
    throw new Error("invalid NOUS action status");
  return data as ActionStatus;
}

/** Scoped HTTPS client for durable actions. Requests only; it can never decide. */
export class ActionHttpClient {
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  constructor(apiOrigin: string, credentials: IntegrationCredentials, fetchFn: typeof fetch = fetch) {
    this.base = apiBase(apiOrigin);
    this.headers = integrationHeaders(credentials);
    this.fetchFn = fetchFn;
  }
  /**
   * Ask NOUS for one action. NOUS validates the arguments, checks the scope and
   * target against the grant, and either runs it or holds it for approval.
   */
  async request(
    toolName: string,
    invocationId: string,
    args: Record<string, unknown>,
  ): Promise<ActionStatus> {
    const invocation: ToolInvocation = {
      tool_name: toolName,
      invocation_id: invocationId,
      arguments: args,
    };
    return parseStatus(await this.send("POST", "/integrations/actions", invocation));
  }
  async requestNote(request: ActionRequest): Promise<ActionStatus> {
    return this.request("create_project_note", request.invocationId, {
      title: request.title,
      content: request.content,
      ...(request.tags ? { tags: request.tags } : {}),
    });
  }
  async status(invocationId: string): Promise<ActionStatus> {
    if (!uuid(invocationId)) throw new Error("invalid invocation id");
    return parseStatus(await this.send("GET", `/integrations/actions/${invocationId}`));
  }
  private async send(method: "GET" | "POST", path: string, body?: object): Promise<unknown> {
    const url = this.base + path;
    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method,
        redirect: "error",
        headers: { ...this.headers, ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: AbortSignal.timeout(30_000),
      });
    } catch (error) {
      const cause =
        error instanceof Error && error.cause instanceof Error
          ? error.cause.message
          : error instanceof Error
            ? error.message
            : String(error);
      const wrapped = new Error(`NOUS request to ${url} failed: ${cause}`);
      console.error(wrapped.message);
      throw wrapped;
    }
    await throwForStatus(response, MESSAGES);
    try {
      return await response.json();
    } catch {
      throw new Error("invalid NOUS response");
    }
  }
}
