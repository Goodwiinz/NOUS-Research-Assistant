export type NativeRequestId = string | number;
export type NativeSession = { id: string };
export type NativeTurn = { id: string };
export type Capabilities = {
  resume: boolean;
  stream: boolean;
  approval: boolean;
  input: boolean;
  cancel: boolean;
  MCP: boolean;
  usage: boolean;
  publish: boolean;
};
export type NativeResponse =
  | { kind: "decision"; allow: boolean }
  | { kind: "answers"; answers: Record<string, string[]> };
export type SessionOptions = {
  cwd: string;
  workspaceId: string;
  policy: {
    sandbox: "workspace-write";
    approvalPolicy: "on-request";
    reviewer: "user";
    networkAccess: false;
    writableRoots: string[];
  };
  // Trusted local composition only. Never deserialize this from bridge commands.
  mcpConfig?: Record<string, { command: string; args: string[] }>;
};
type Position = { sessionId: string; turnId: string };
export type AdapterEvent =
  | (Position & { kind: "delta"; text: string })
  | (Position & {
      kind: "tool";
      itemId: string;
      name: string;
      phase: "started" | "completed";
      status?: "completed" | "failed" | "declined";
      preview?: string;
    })
  | (Position & {
      kind: "request";
      itemId: string;
      requestId: NativeRequestId;
      approvalId?: string;
      method: string;
      params: Record<string, unknown>;
    })
  | (Position & { kind: "usage"; inputTokens: number; outputTokens: number })
  | (Position & {
      kind: "terminal";
      status: "completed" | "failed" | "interrupted";
    });
export interface HarnessAdapter {
  probe(): Promise<Capabilities>;
  startSession(options: SessionOptions): Promise<NativeSession>;
  resumeSession(id: string, options: SessionOptions): Promise<NativeSession>;
  startTurn(
    sessionId: string,
    input: string,
    commandId: string,
  ): Promise<NativeTurn>;
  interruptTurn(sessionId: string, turnId: string): Promise<void>;
  respondToRequest(
    id: NativeRequestId,
    response: NativeResponse,
  ): Promise<void>;
  closeSession(): Promise<void>;
  events(signal: AbortSignal): AsyncIterable<AdapterEvent>;
}
