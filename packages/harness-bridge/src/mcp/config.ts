import { fileURLToPath } from "node:url";
import type { SessionOptions } from "../contracts.ts";
import type { McpSession } from "./client.ts";

const cliPath = fileURLToPath(new URL("../cli.ts", import.meta.url));
// Absolute loader path: Codex spawns MCP servers from the user's workspace,
// where a bare `tsx` specifier would not resolve.
const tsxLoader = fileURLToPath(import.meta.resolve("tsx"));

function mcpArgs(session: McpSession): string[] {
  return [
    "--import",
    tsxLoader,
    cliPath,
    "mcp",
    "--api",
    session.apiOrigin,
    "--store",
    session.stateDir,
    "--session",
    session.credentialHandle,
  ];
}
/** Session-scoped Codex MCP configuration; only opaque handles reach argv. */
export function buildManagedMcpConfig(
  session: McpSession,
): NonNullable<SessionOptions["mcpConfig"]> {
  return { nous: { command: process.execPath, args: mcpArgs(session) } };
}
/** Printed for the user to run; this package never edits global Codex config. */
export function standaloneInstallCommand(session: McpSession): string {
  const quote = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`;
  return [
    "codex mcp add nous --",
    quote(process.execPath),
    ...mcpArgs(session).map(quote),
  ].join(" ");
}
