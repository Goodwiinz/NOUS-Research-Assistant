import assert from "node:assert/strict";
import { test } from "node:test";
import type {
  Capabilities,
  HarnessAdapter,
  NativeObservation,
  NativeResponse,
} from "../src/contracts.ts";

const codexCapabilities: Capabilities = {
  resume: true,
  stream: true,
  approval: true,
  input: true,
  cancel: true,
  MCP: true,
  usage: true,
  publish: false,
};

const textOnlyCapabilities: Capabilities = {
  resume: false,
  stream: true,
  approval: false,
  input: false,
  cancel: false,
  MCP: false,
  usage: false,
  publish: false,
};

const capabilityOperation: Record<
  keyof Capabilities,
  keyof HarnessAdapter | "publishArtifact"
> = {
  resume: "resumeSession",
  stream: "events",
  approval: "respondToRequest",
  input: "respondToRequest",
  cancel: "interruptTurn",
  MCP: "startSession",
  usage: "events",
  publish: "publishArtifact",
};

/** Ensure every advertised capability has a callable adapter operation. */
export async function assertAdapterConformance(
  adapter: HarnessAdapter,
): Promise<void> {
  const capabilities = await adapter.probe();
  for (const capability of Object.keys(capabilityOperation) as Array<
    keyof Capabilities
  >) {
    const advertised = capabilities[capability];
    if (typeof advertised !== "boolean") {
      throw new Error(`invalid ${capability} capability declaration`);
    }
    if (!advertised) continue;
    const operation = capabilityOperation[capability as keyof Capabilities];
    if (
      typeof (adapter as unknown as Record<string, unknown>)[operation] !==
      "function"
    ) {
      throw new Error(
        `${capability} capability advertised without callable ${operation} behavior`,
      );
    }
  }
}

function adapterFixture(
  capabilities: Capabilities,
  overrides: Record<string, unknown> = {},
): HarnessAdapter {
  const fixture: HarnessAdapter = {
    probe: async () => capabilities,
    startSession: async () => ({ id: "fixture-session" }),
    resumeSession: async (id) => ({ id }),
    startTurn: async () => ({ id: "fixture-turn" }),
    interruptTurn: async () => {},
    respondToRequest: async (
      _id: string | number,
      _response: NativeResponse,
    ) => {},
    inspectTurn: async (sessionId): Promise<NativeObservation> => ({
      state: "unknown",
      sessionId,
      turnId: null,
    }),
    closeSession: async () => {},
    events: async function* () {},
    ...overrides,
  } as HarnessAdapter;
  return fixture;
}

const codexFixtureAdapter = adapterFixture(codexCapabilities);
const falseResumeAdapter = adapterFixture(codexCapabilities, {
  resumeSession: undefined,
});
const textOnlyAdapter = adapterFixture(textOnlyCapabilities, {
  resumeSession: undefined,
  respondToRequest: undefined,
  interruptTurn: undefined,
});

test("capabilities match the adapter's callable behavior", async () => {
  await assert.rejects(assertAdapterConformance(falseResumeAdapter), /resume/);
  await assertAdapterConformance(codexFixtureAdapter);
  await assertAdapterConformance(textOnlyAdapter);
});

test("text-only adapters do not advertise resume or approval", async () => {
  assert.deepEqual(await textOnlyAdapter.probe(), textOnlyCapabilities);
  await assertAdapterConformance(textOnlyAdapter);
});
