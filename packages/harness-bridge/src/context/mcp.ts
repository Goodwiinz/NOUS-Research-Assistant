import { integrationHeaders, type IntegrationCredentials } from "../credentials.ts";
import { apiBase, throwForStatus, type ToolResult, ToolRequestRejected } from "../mcp/client.ts";
import type { LocalTool } from "../mcp/server.ts";

const MESSAGES = {
  forbidden:
    "NOUS denied context access: the grant may lack context:read (reconnect with nous-harness connect --tools --context) or the project is outside the grant",
};

/** Reads the memories the user chose to share; it can never change the choice. */
export class ContextHttpClient {
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  constructor(apiOrigin: string, credentials: IntegrationCredentials, fetchFn: typeof fetch = fetch) {
    this.base = apiBase(apiOrigin);
    this.headers = integrationHeaders(credentials);
    this.fetchFn = fetchFn;
  }
  /** One page of the selection; without an offset the server reads page 0. */
  async read(offset?: number): Promise<ToolResult> {
    return this.request(offset === undefined ? "" : `?offset=${offset}`);
  }
  async load(skillName: string): Promise<ToolResult> {
    return this.request("/skills/load", { skill_name: skillName });
  }
  private async request(path: string, body?: { skill_name: string }): Promise<ToolResult> {
    const url = `${this.base}/integrations/context${path}`;
    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method: body ? "POST" : "GET",
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
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      throw new Error("invalid NOUS response");
    }
    if (
      typeof data !== "object" ||
      data === null ||
      !Array.isArray((data as ToolResult).content) ||
      !Array.isArray((data as ToolResult).source_refs) ||
      typeof (data as ToolResult).is_error !== "boolean"
    )
      throw new Error("invalid NOUS context result");
    return data as ToolResult;
  }
}

/** A selection holds at most 25 memories (the server's MAX_SELECTED_MEMORIES), so no offset past 25 is a page. */
const MAX_CONTEXT_OFFSET = 25;

function contextOffset(args: Record<string, unknown>): number | undefined {
  const { offset } = args;
  if (Object.keys(args).every((key) => key === "offset")) {
    if (offset === undefined) return undefined;
    if (typeof offset === "number" && Number.isInteger(offset) && offset >= 0 && offset <= MAX_CONTEXT_OFFSET)
      return offset;
  }
  throw new ToolRequestRejected(
    `NOUS rejected the read_selected_context arguments: offset must be an integer from 0 to ${MAX_CONTEXT_OFFSET}`,
  );
}

/** `read_selected_context`: the project memories the user chose to share with this device. */
export function readSelectedContextTool(client: ContextHttpClient): LocalTool {
  return {
    descriptor: {
      name: "read_selected_context",
      description:
        "Read the project memories and frozen skill catalog the user chose to share with this device. Load a listed skill with load_selected_skill. Returns nothing until the user selects context in NOUS. Results are paged: when next_offset is not null, call again with offset set to next_offset until it is null.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        properties: { offset: { type: "integer", minimum: 0, maximum: MAX_CONTEXT_OFFSET } },
      },
    },
    async call(args) {
      const result = await client.read(contextOffset(args));
      const structured = { content: result.content, source_refs: result.source_refs };
      return {
        text: JSON.stringify(structured),
        structured,
        ...(result.is_error ? { isError: true } : {}),
      };
    },
  };
}


/** Only the backend's browser-frozen catalog can authorize a skill document. */
export function loadSelectedSkillTool(client: ContextHttpClient): LocalTool {
  return {
    descriptor: {
      name: "load_selected_skill",
      description: "Read the exact frozen instructions for one skill listed by read_selected_context. Limited to three skills and 12,000 instruction tokens per shared snapshot.",
      inputSchema: { type: "object", additionalProperties: false, required: ["skill_name"], properties: { skill_name: { type: "string", minLength: 1, maxLength: 128 } } },
    },
    async call(args) {
      if (Object.keys(args).some(key => key !== "skill_name") || typeof args.skill_name !== "string" || args.skill_name.length < 1 || args.skill_name.length > 128)
        throw new ToolRequestRejected("NOUS rejected the skill name");
      const result = await client.load(args.skill_name);
      const structured = { content: result.content, source_refs: result.source_refs };
      return { text: JSON.stringify(structured), structured, ...(result.is_error ? { isError: true } : {}) };
    },
  };
}
