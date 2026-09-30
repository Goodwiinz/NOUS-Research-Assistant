import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ArtifactHttpClient } from "../artifacts/client.ts";
import { artifactsPublishTool } from "../artifacts/mcp.ts";
import { createArtifactPublisher } from "../artifacts/publisher.ts";
import { grantedRoot } from "../artifacts/snapshot.ts";
import { CredentialStore } from "../credentials.ts";
import { CapabilityClient, type McpSession } from "./client.ts";
import { createNousMcpServer, type LocalTool } from "./server.ts";

/** Serves NOUS read tools (and publication when a root is bound) over stdio. */
export async function runStdioMcp(session: McpSession): Promise<void> {
  const credentials = await new CredentialStore(session.stateDir).load(
    session.credentialHandle,
  );
  const local: LocalTool[] = [];
  if (session.outputRoot) {
    // The root is pinned now; a later swap of the directory is refused per call.
    try {
      const root = await grantedRoot(session.outputRoot);
      local.push(
        artifactsPublishTool(
          createArtifactPublisher(
            new ArtifactHttpClient(session.apiOrigin, credentials),
            root,
          ),
        ),
      );
    } catch (error) {
      // Read tools stay available; publication is simply not offered.
      console.error(
        `artifacts_publish unavailable: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
  const server = createNousMcpServer(
    new CapabilityClient(session.apiOrigin, credentials),
    local,
  );
  // Diagnostics go to stderr only; stdout is the JSON-RPC channel.
  server.onerror = (error) =>
    console.error(error instanceof Error ? error.message : String(error));
  const transport = new StdioServerTransport();
  await server.connect(transport);
  await new Promise<void>((resolve) => {
    server.onclose = resolve;
    // The SDK transport does not watch stdin EOF; Codex closing the pipe ends us.
    process.stdin.once("end", resolve);
  });
  await server.close();
}
