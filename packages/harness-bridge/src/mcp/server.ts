import { randomUUID } from "node:crypto";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import {
  ReauthenticationRequired,
  ToolRequestRejected,
  type CapabilityClient,
  type ToolDescriptor,
} from "./client.ts";

/** A tool served locally by this process (e.g. artifact publication). */
export type LocalTool = {
  descriptor: ToolDescriptor;
  call(args: Record<string, unknown>): Promise<{
    text: string;
    structured?: Record<string, unknown>;
    isError?: boolean;
  }>;
};

/**
 * Low-level Server so the backend's JSON Schema catalog passes through
 * unchanged. The backend owns authorization, allowlists, and argument
 * validation for read tools; local tools validate their own arguments and
 * never take identity, root or credentials from the model.
 */
export function createNousMcpServer(
  client: CapabilityClient,
  localTools: LocalTool[] = [],
): Server {
  const server = new Server(
    { name: "nous", version: "0.1.0" },
    { capabilities: { tools: {} } },
  );
  const local = new Map(localTools.map((tool) => [tool.descriptor.name, tool]));
  server.setRequestHandler(ListToolsRequestSchema, async () => {
    let listed: ToolDescriptor[] = [];
    try {
      listed = await client.listTools();
    } catch (error) {
      // A read-gateway outage must not hide tools served locally.
      if (localTools.length === 0) throw error;
      console.error(
        `NOUS tool catalog unavailable: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    const catalog = listed.filter((tool) => {
      if (!local.has(tool.name)) return true;
      // A local tool always wins; never advertise two tools with one name.
      console.error(`NOUS catalog tool ${tool.name} shadowed by the local tool`);
      return false;
    });
    return { tools: [...catalog, ...localTools.map((t) => t.descriptor)] };
  });
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    try {
      const tool = local.get(request.params.name);
      if (tool) {
        const outcome = await tool.call(request.params.arguments ?? {});
        return {
          content: [{ type: "text", text: outcome.text }],
          ...(outcome.structured ? { structuredContent: outcome.structured } : {}),
          ...(outcome.isError ? { isError: true } : {}),
        };
      }
      const result = await client.invokeRead({
        tool_name: request.params.name,
        arguments: request.params.arguments ?? {},
        invocation_id: randomUUID(),
      });
      const structuredContent = {
        content: result.content,
        source_refs: result.source_refs,
      };
      return {
        content: [{ type: "text", text: JSON.stringify(structuredContent) }],
        structuredContent,
        isError: result.is_error,
      };
    } catch (error) {
      if (
        error instanceof ReauthenticationRequired ||
        error instanceof ToolRequestRejected
      ) {
        // Also record auth/disabled conditions in Codex's MCP stderr log.
        console.error(error.message);
        return { content: [{ type: "text", text: error.message }], isError: true };
      }
      console.error(
        `tool ${request.params.name} failed: ${error instanceof Error ? error.message : String(error)}`,
      );
      throw error;
    }
  });
  return server;
}
