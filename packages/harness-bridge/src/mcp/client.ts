import type { components } from "../../../../frontend/src/types/generated/api.d.ts";
import {
  integrationHeaders,
  type IntegrationCredentials,
} from "../credentials.ts";

export type ToolDescriptorDTO = components["schemas"]["ToolDescriptorDTO"];
export type ToolInvocation = components["schemas"]["ToolInvocation"];
export type ToolResult = components["schemas"]["ToolResult"];
export type ToolDescriptor = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
};
/** Opaque local handles only; tokens never travel through argv or config. */
export type McpSession = {
  apiOrigin: string;
  credentialHandle: string;
  stateDir: string;
  // Absolute registered output root; enables artifacts_publish when present.
  outputRoot?: string;
  // Grant carries context:read; enables read_selected_context.
  context?: boolean;
  // Grant carries tools:write (either binding); enables request_action /
  // get_action_status.
  actions?: boolean;
  // Grant carries handoff:read/handoff:write; enables get/save_nous_handoff.
  handoff?: boolean;
  // Grant carries library:write (which needs tools:write); also enables
  // request_action / get_action_status.
  library?: boolean;
};
export const REAUTH_MESSAGE =
  "NOUS session expired or revoked; reconnect this device (nous-harness connect --tools)";
export const FORBIDDEN_MESSAGE =
  "NOUS denied this tool call: the grant may lack tools:read (reconnect with nous-harness connect --tools) or the resource is outside the granted project";
export const DISABLED_MESSAGE = "NOUS integration tools are disabled";
export class ReauthenticationRequired extends Error {
  constructor() {
    super(REAUTH_MESSAGE);
  }
}
/** Stable tool-level errors (403/422/503); never retried. */
export class ToolRequestRejected extends Error {}
/** NOUS bounds search_arxiv at 120 s and get_arxiv_paper_content at 125 s (a
 * 120 s budget plus a 5 s outer guard; backend read_tools.py); every other read
 * keeps a short bound so a hung call does not block Codex for minutes. */
export const READ_TIMEOUT_MS = 30_000;
export const SLOW_READ_TIMEOUT_MS = 135_000;
const SLOW_READ_TOOLS = new Set(["search_arxiv", "get_arxiv_paper_content"]);
/** Codex's own per-call MCP limit (60 s by default) must outlast the client. */
export const MCP_TOOL_TIMEOUT_SEC = 150;
/** The stable tool error NOUS itself returns when a tool's budget runs out. */
const UPSTREAM_TIMEOUT: ToolResult = {
  content: [{ error: "upstream_timeout" }],
  is_error: true,
  source_refs: [],
};
class RequestTimedOut extends Error {}
const timedOut = (error: unknown) =>
  error instanceof Error && error.name === "TimeoutError";

/** HTTPS only, except the explicit development loopback; no embedded credentials. */
export function apiBase(value: string): string {
  const url = new URL(value);
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    (url.protocol !== "https:" &&
      !(
        url.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      ))
  )
    throw new Error(
      "API requires HTTPS (or local loopback) without credentials",
    );
  return value.replace(/\/$/, "");
}
/** Backend `detail` strings are safe stable messages; anything else is dropped. */
async function detailOf(response: Response): Promise<string | undefined> {
  try {
    const data: unknown = await response.json();
    const detail =
      typeof data === "object" && data !== null
        ? (data as { detail?: unknown }).detail
        : undefined;
    return typeof detail === "string" && detail.length <= 500
      ? detail
      : undefined;
  } catch {
    return undefined;
  }
}

export type StatusMessages = { forbidden?: string; disabled?: string };

/** Map gateway statuses to stable tool errors; 2xx passes through. */
export async function throwForStatus(
  response: Response,
  messages: StatusMessages = {},
): Promise<void> {
  if (response.status === 401) throw new ReauthenticationRequired();
  if (response.status === 403)
    throw new ToolRequestRejected(messages.forbidden ?? FORBIDDEN_MESSAGE);
  if (response.status === 404)
    throw new ToolRequestRejected(
      (await detailOf(response)) ?? "NOUS could not find that resource",
    );
  if (response.status === 409)
    throw new ToolRequestRejected(
      (await detailOf(response)) ?? "NOUS reported a conflict",
    );
  if (response.status === 413)
    throw new ToolRequestRejected(
      (await detailOf(response)) ?? "NOUS rejected the size or quota",
    );
  if (response.status === 422) {
    const detail = await detailOf(response);
    throw new ToolRequestRejected(
      "NOUS rejected the tool arguments" + (detail ? `: ${detail}` : ""),
    );
  }
  if (response.status === 503)
    throw new ToolRequestRejected(
      (await detailOf(response)) ?? messages.disabled ?? DISABLED_MESSAGE,
    );
  if (!response.ok) {
    const detail = await detailOf(response);
    throw new Error(
      `NOUS request failed (${response.status})` + (detail ? `: ${detail}` : ""),
    );
  }
}

/** Scoped HTTPS facade over the backend read gateway. Reads only; never retries. */
export class CapabilityClient {
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  private readonly readMs: number;
  private readonly slowReadMs: number;
  constructor(
    apiOrigin: string,
    credentials: IntegrationCredentials,
    fetchFn: typeof fetch = fetch,
    // Test seam; production uses READ_TIMEOUT_MS / SLOW_READ_TIMEOUT_MS.
    timing: { readMs?: number; slowReadMs?: number } = {},
  ) {
    this.base = apiBase(apiOrigin);
    this.headers = integrationHeaders(credentials);
    this.fetchFn = fetchFn;
    this.readMs = timing.readMs ?? READ_TIMEOUT_MS;
    this.slowReadMs = timing.slowReadMs ?? SLOW_READ_TIMEOUT_MS;
  }
  async listTools(): Promise<ToolDescriptor[]> {
    const data = await this.request("/integrations/tools");
    if (!Array.isArray(data)) throw new Error("invalid NOUS tool catalog");
    return (data as ToolDescriptorDTO[]).map((tool) => ({
      name: tool.name,
      description: tool.description,
      inputSchema: tool.input_schema,
    }));
  }
  async invokeRead(invocation: ToolInvocation): Promise<ToolResult> {
    let data: unknown;
    try {
      data = await this.request(
        "/integrations/tools/read",
        {
          tool_name: invocation.tool_name,
          arguments: invocation.arguments ?? {},
          invocation_id: invocation.invocation_id,
        },
        SLOW_READ_TOOLS.has(invocation.tool_name) ? this.slowReadMs : this.readMs,
      );
    } catch (error) {
      // The same stable tool error NOUS returns when its own budget runs out,
      // so the model gets a result it can act on, not a JSON-RPC failure.
      if (error instanceof RequestTimedOut)
        return structuredClone(UPSTREAM_TIMEOUT);
      throw error;
    }
    if (
      typeof data !== "object" ||
      data === null ||
      !Array.isArray((data as ToolResult).content) ||
      !Array.isArray((data as ToolResult).source_refs) ||
      typeof (data as ToolResult).is_error !== "boolean"
    )
      throw new Error("invalid NOUS tool result");
    return data as ToolResult;
  }
  private async request(
    path: string,
    body?: object,
    timeoutMs: number = this.readMs,
  ): Promise<unknown> {
    const url = this.base + path;
    let response: Response;
    try {
      response = await this.fetchFn(url, {
        method: body ? "POST" : "GET",
        redirect: "error",
        headers: {
          ...this.headers,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      if (timedOut(error)) {
        console.error(`NOUS request to ${url} timed out after ${timeoutMs / 1000} s`);
        throw new RequestTimedOut(`NOUS request to ${url} timed out`);
      }
      // undici hides ECONNREFUSED/ENOTFOUND/TLS reasons in `cause`.
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
    await throwForStatus(response);
    try {
      return await response.json();
    } catch (error) {
      if (timedOut(error)) {
        console.error(`NOUS request to ${url} timed out after ${timeoutMs / 1000} s`);
        throw new RequestTimedOut(`NOUS request to ${url} timed out`);
      }
      throw new Error("invalid NOUS response");
    }
  }
}
