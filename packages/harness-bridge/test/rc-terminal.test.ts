import assert from "node:assert/strict";
import { test } from "node:test";
import { PassThrough } from "node:stream";
import { setImmediate as tick } from "node:timers/promises";
import { terminalPrompt } from "../src/cli.ts";

function streams(tty = false) {
  const input = new PassThrough();
  const output = new PassThrough();
  // readline only interprets Ctrl-C as a key when the output is a terminal.
  if (tty) Object.assign(output, { isTTY: true });
  return { input, output };
}

test("an answered question resolves with the line", async () => {
  const { input, output } = streams();
  const terminal = terminalPrompt(input, output);
  const answer = terminal.prompt("Project number: ");
  input.write("2\n");
  assert.equal(await answer, "2");
  terminal.close();
});

test("input that ends while a question is open rejects with Cancelled", async () => {
  const { input, output } = streams();
  const terminal = terminalPrompt(input, output);
  const answer = terminal.prompt("Project number: ");
  input.end();
  await assert.rejects(answer, { message: "Cancelled" });
});

test("Ctrl-C while a question is open rejects with Cancelled", async () => {
  const { input, output } = streams(true);
  const terminal = terminalPrompt(input, output);
  const answer = terminal.prompt("Project number: ");
  input.write("\x03");
  await assert.rejects(answer, { message: "Cancelled" });
});

test("close() with nothing pending causes no unhandled rejection", async () => {
  const unhandled: unknown[] = [];
  const listener = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", listener);
  try {
    const { input, output } = streams();
    const terminal = terminalPrompt(input, output);
    terminal.close();
    terminal.close();
    await tick();
    await tick();
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", listener);
  }
});

test("prompt after close() rejects with Cancelled", async () => {
  const { input, output } = streams();
  const terminal = terminalPrompt(input, output);
  terminal.close();
  await assert.rejects(terminal.prompt("Project number: "), {
    message: "Cancelled",
  });
});
