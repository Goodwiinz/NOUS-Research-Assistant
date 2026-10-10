import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ActionHttpClient } from "../actions/client.ts";
import { actionStatusTool, requestActionTool } from "../actions/mcp.ts";
import { ArtifactHttpClient } from "../artifacts/client.ts";
import { ContextHttpClient, readSelectedContextTool, loadSelectedSkillTool } from "../context/mcp.ts";
import { artifactsPublishTool, unavailablePublishTool } from "../artifacts/mcp.ts";
import { createArtifactPublisher } from "../artifacts/publisher.ts";
import { grantedRoot } from "../artifacts/snapshot.ts";
import { CredentialStore } from "../credentials.ts";
import { GrantKeeper } from "../grants.ts";
import { HandoffHttpClient } from "../handoffs/client.ts";
import { getHandoffTool, saveHandoffTool } from "../handoffs/mcp.ts";
import { HandoffQueue, localBinding } from "../handoffs/queue.ts";
import { CapabilityClient, type McpSession } from "./client.ts";
import { createNousMcpServer, type LocalTool } from "./server.ts";

/** Serves NOUS read tools, plus publication, action and handoff tools when granted, over stdio. */
export async function runStdioMcp(session: McpSession): Promise<void> {
  // Every client sends the keeper's current grant, renewed before expiry.
  const store = new CredentialStore(session.stateDir);
  const keeper = new GrantKeeper(
    store,
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
  // --actions (tools:write, either binding) or --library (library:write)
  // offers the action tools; NOUS checks each action's own scope.
  if (session.actions || session.library) {
    // Requests only: approval stays a browser action the model cannot take.
    const actions = new ActionHttpClient(session.apiOrigin, credentials, keeper.fetch);
    local.push(requestActionTool(actions), actionStatusTool(actions));
  }
  if (session.handoff) {
    // The chat is the grant's binding; the model never names it.
    const handoffs = new HandoffHttpClient(session.apiOrigin, credentials, keeper.fetch);
    // Saves are journaled in the same store; the binding is re-read per save.
    local.push(
      getHandoffTool(handoffs),
      saveHandoffTool(new HandoffQueue(store, handoffs), () => localBinding(store, session.credentialHandle)),
    );
  }
  if (session.context) {
    const context = new ContextHttpClient(session.apiOrigin, credentials, keeper.fetch);
    local.push(readSelectedContextTool(context), loadSelectedSkillTool(context));
  }
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
