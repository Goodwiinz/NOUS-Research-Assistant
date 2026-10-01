import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ArtifactHttpClient } from "../artifacts/client.ts";
import { ContextHttpClient, readSelectedContextTool } from "../context/mcp.ts";
import { artifactsPublishTool, unavailablePublishTool } from "../artifacts/mcp.ts";
import { createArtifactPublisher } from "../artifacts/publisher.ts";
import { grantedRoot } from "../artifacts/snapshot.ts";
import { CredentialStore } from "../credentials.ts";
import { GrantKeeper } from "../grants.ts";
import { CapabilityClient, type McpSession } from "./client.ts";
import { createNousMcpServer, type LocalTool } from "./server.ts";

/** Serves NOUS read tools (and publication when a root is bound) over stdio. */
export async function runStdioMcp(session: McpSession): Promise<void> {
  // Every client sends the keeper's current grant, renewed before expiry.
  const keeper = new GrantKeeper(
    new CredentialStore(session.stateDir),
    session.credentialHandle,
    session.apiOrigin,
  );
  const credentials = await keeper.current();
  const stopRenewal = keeper.keepFresh((error) =>
    console.error(error instanceof Error ? error.message : String(error)),
  );
  const local: LocalTool[] = [];
  if (session.outputRoot) {
    // The root is pinned now; a later swap of the directory is refused per call.
    try {
      const root = await grantedRoot(session.outputRoot);
      const publisher = createArtifactPublisher(
        new ArtifactHttpClient(session.apiOrigin, credentials, keeper.fetch),
        root,
      );
      // Reserve, upload and finalize must all use the reserving grant.
      local.push(
        artifactsPublishTool({
          publish: (input) => keeper.pinned(() => publisher.publish(input)),
        }),
      );
    } catch (error) {
      // Read tools stay available; the publish tool stays visible and says why.
      const reason = error instanceof Error ? error.message : String(error);
      console.error(`artifacts_publish unavailable: ${reason}`);
      local.push(unavailablePublishTool(session.outputRoot, reason));
    }
  }
  if (session.context)
    local.push(
      readSelectedContextTool(new ContextHttpClient(session.apiOrigin, credentials)),
    );
  const server = createNousMcpServer(
    new CapabilityClient(session.apiOrigin, credentials, keeper.fetch),
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
  stopRenewal();
  await server.close();
}
