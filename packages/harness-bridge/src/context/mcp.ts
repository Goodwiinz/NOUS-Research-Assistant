import { integrationHeaders, type IntegrationCredentials } from "../credentials.ts";
import { apiBase, throwForStatus, type ToolResult } from "../mcp/client.ts";
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
  async read(): Promise<ToolResult> {
    const url = `${this.base}/integrations/context`;
    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method: "GET",
        redirect: "error",
        headers: this.headers,
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

/** `read_selected_context`: the project memories the user chose to share with this device. */
export function readSelectedContextTool(client: ContextHttpClient): LocalTool {
  return {
    descriptor: {
      name: "read_selected_context",
      description:
        "Read the NOUS project memories the user chose to share with this device (terse facts and preferences). Returns nothing until the user selects memories in NOUS.",
      inputSchema: { type: "object", additionalProperties: false, properties: {} },
    },
    async call() {
      const result = await client.read();
      const structured = { content: result.content, source_refs: result.source_refs };
      return {
        text: JSON.stringify(structured),
        structured,
        ...(result.is_error ? { isError: true } : {}),
      };
    },
  };
}
