import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CredentialStore } from "../credentials.ts";
import { CapabilityClient, type McpSession } from "./client.ts";
import { createNousMcpServer } from "./server.ts";

/** Serves NOUS read tools over stdio until the client closes the pipe. */
export async function runStdioMcp(session: McpSession): Promise<void> {
  const credentials = await new CredentialStore(session.stateDir).load(
    session.credentialHandle,
  );
  const server = createNousMcpServer(
    new CapabilityClient(session.apiOrigin, credentials),
  );
  // Diagnostics go to stderr only; stdout is the JSON-RPC channel.
  server.onerror = (error) =>
    console.error(error instanceof Error ? error.message : String(error));
  const transport = new StdioServerTransport();
  await server.connect(transport);
  await new Promise<void>((resolve) => {
    server.onclose = resolve;
  });
}
