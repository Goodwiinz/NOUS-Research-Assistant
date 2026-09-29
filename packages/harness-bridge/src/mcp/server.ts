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
} from "./client.ts";

/**
 * Low-level Server so the backend's JSON Schema catalog passes through
 * unchanged. The backend owns authorization, allowlists, and argument
 * validation; this facade only forwards the model's arguments in the body.
 */
export function createNousMcpServer(client: CapabilityClient): Server {
  const server = new Server(
    { name: "nous", version: "0.1.0" },
    { capabilities: { tools: {} } },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: await client.listTools(),
  }));
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    try {
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
      )
        return { content: [{ type: "text", text: error.message }], isError: true };
      throw error;
    }
  });
  return server;
}
