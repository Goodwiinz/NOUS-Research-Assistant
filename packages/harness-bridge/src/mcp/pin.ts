import { ToolRequestRejected, type ToolDescriptor, type ToolInvocation, type ToolResult } from "./client.ts";
import type { ToolCatalog } from "./server.ts";

/** Fields that name a NOUS project in a tool or action call. */
export const PROJECT_FIELDS = ["project_id", "from_project_id", "to_project_id"] as const;
/** Read tools that reach beyond one project. */
const ACCOUNT_WIDE_TOOLS = new Set(["list_library"]);

export const pinnedMessage = (project: string): string =>
  `this Local Codex run can use NOUS project ${project} only (its chat's project)`;

/**
 * A pinned call's arguments: every project field must name the pinned
 * project, and `project_id` is filled in when the call takes one and leaves it
 * out. Returns the refusal text otherwise.
 */
export function pinnedArguments(
  project: string,
  args: Record<string, unknown>,
  takesProjectId: boolean,
): Record<string, unknown> | string {
  const pinned = { ...args };
  for (const field of PROJECT_FIELDS) {
    const value = pinned[field];
    if (value === undefined || value === null) continue;
    if (typeof value !== "string" || value.toLowerCase() !== project.toLowerCase())
      return `${pinnedMessage(project)}; ${field} names another project`;
  }
  if (takesProjectId && (pinned.project_id === undefined || pinned.project_id === null))
    pinned.project_id = project;
  return pinned;
}

/** project_id as a pinned session advertises it: the run's project, never required. */
export function pinnedProperty(project: string): Record<string, unknown> {
  return {
    type: "string",
    enum: [project],
    description: "This run's NOUS project (its chat's); filled in when left out.",
  };
}

function pinnedDescriptor(project: string, tool: ToolDescriptor): ToolDescriptor {
  const properties = (tool.inputSchema.properties ?? {}) as Record<string, unknown>;
  if (!("project_id" in properties)) return tool;
  const required = tool.inputSchema.required;
  return {
    ...tool,
    inputSchema: {
      ...tool.inputSchema,
      properties: { ...properties, project_id: pinnedProperty(project) },
      ...(Array.isArray(required) ? { required: required.filter((key) => key !== "project_id") } : {}),
    },
  };
}

/**
 * The NOUS read tools of a Local Codex run under an account pairing, pinned
 * to the run's project (account-connection design §3). Not a security
 * boundary: the MCP child holds the account credential. It keeps the model
 * inside the chat's project; account-wide reach stays with a Codex the user
 * starts (`nous-harness mcp install`).
 */
export function pinCatalog(catalog: ToolCatalog, project: string): ToolCatalog {
  let takesProject: Set<string> | null = null;
  const listTools = async (): Promise<ToolDescriptor[]> => {
    const tools = (await catalog.listTools()).filter((tool) => !ACCOUNT_WIDE_TOOLS.has(tool.name));
    takesProject = new Set(
      tools
        .filter((tool) => "project_id" in ((tool.inputSchema.properties ?? {}) as object))
        .map((tool) => tool.name),
    );
    return tools.map((tool) => pinnedDescriptor(project, tool));
  };
  return {
    listTools,
    async invokeRead(invocation: ToolInvocation): Promise<ToolResult> {
      if (ACCOUNT_WIDE_TOOLS.has(invocation.tool_name))
        throw new ToolRequestRejected(`${pinnedMessage(project)}; ${invocation.tool_name} reaches the whole account`);
      if (takesProject === null) await listTools();
      const args = pinnedArguments(
        project,
        (invocation.arguments ?? {}) as Record<string, unknown>,
        takesProject!.has(invocation.tool_name),
      );
      if (typeof args === "string") throw new ToolRequestRejected(args);
      return catalog.invokeRead({ ...invocation, arguments: args });
    },
  };
}
