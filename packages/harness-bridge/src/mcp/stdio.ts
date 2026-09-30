import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ActionHttpClient } from "../actions/client.ts";
import { actionStatusTool, requestActionTool } from "../actions/mcp.ts";
import { ArtifactHttpClient } from "../artifacts/client.ts";
import { artifactsPublishTool, unavailablePublishTool } from "../artifacts/mcp.ts";
import { createArtifactPublisher } from "../artifacts/publisher.ts";
import { grantedRoot } from "../artifacts/snapshot.ts";
import { CredentialStore } from "../credentials.ts";
import { CapabilityClient, type McpSession } from "./client.ts";
import { createNousMcpServer, type LocalTool } from "./server.ts";

/** Serves NOUS read tools, plus publication and action requests when granted, over stdio. */
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
      // Read tools stay available; the publish tool stays visible and says why.
      const reason = error instanceof Error ? error.message : String(error);
      console.error(`artifacts_publish unavailable: ${reason}`);
      local.push(unavailablePublishTool(session.outputRoot, reason));
    }
  }
  if (session.actions) {
    // Requests only: approval stays a browser action the model cannot take.
    const actions = new ActionHttpClient(session.apiOrigin, credentials);
    local.push(requestActionTool(actions), actionStatusTool(actions));
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
