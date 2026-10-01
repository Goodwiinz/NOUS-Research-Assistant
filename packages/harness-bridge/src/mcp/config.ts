import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import type { SessionOptions } from "../contracts.ts";
import type { McpSession } from "./client.ts";

const cliPath = fileURLToPath(new URL("../cli.ts", import.meta.url));

function mcpArgs(session: McpSession): string[] {
  // Codex spawns MCP servers from the user's workspace, so both the loader and
  // the store must be absolute; a bare `tsx` or `./store` would not resolve.
  if (!isAbsolute(session.stateDir))
    throw new Error("MCP state directory must be absolute");
  if (session.outputRoot !== undefined && !isAbsolute(session.outputRoot))
    throw new Error("MCP output root must be absolute");
  const tsxLoader = fileURLToPath(import.meta.resolve("tsx"));
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
    ...(session.outputRoot ? ["--root", session.outputRoot] : []),
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
