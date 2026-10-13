import assert from "node:assert/strict";
import { test } from "node:test";
import { PassThrough } from "node:stream";
import { setImmediate as tick } from "node:timers/promises";
import { stripVTControlCharacters } from "node:util";
import { terminalPrompt } from "../src/cli.ts";

function streams(tty = false) {
  const input = new PassThrough();
  const output = new PassThrough();
  // readline only interprets Ctrl-C and Ctrl-D as keys when the output is a
  // terminal.
  if (tty) Object.assign(output, { isTTY: true });
  let written = "";
  output.on("data", (chunk) => (written += String(chunk)));
  return { input, output, written: () => written };
}

/** A terminal prompt over fake streams that counts `onInterrupt` calls. */
function counted(tty = false) {
  const s = streams(tty);
  let interrupts = 0;
  const terminal = terminalPrompt(s.input, s.output, () => (interrupts += 1));
  return { ...s, terminal, interrupts: () => interrupts };
}

test("an answered question resolves with the line", async () => {
  const { input, terminal, interrupts } = counted();
  const answer = terminal.prompt("Project number: ");
  input.write("2\n");
  assert.equal(await answer, "2");
  terminal.close();
  assert.equal(interrupts(), 0);
});

test("input that ends while a question is open rejects with Cancelled and cancels the command", async () => {
  const { input, terminal, interrupts } = counted();
  const answer = terminal.prompt("Project number: ");
  input.end();
  await assert.rejects(answer, { message: "Cancelled" });
  assert.equal(interrupts(), 1);
});

test("input that ends with no question open cancels the command, once", async () => {
  const { input, interrupts } = counted();
  input.end();
  await tick();
  await tick();
  assert.equal(interrupts(), 1);
});

test("Ctrl-D while a question is open rejects with Cancelled and cancels the command, once", async () => {
  const { input, terminal, interrupts } = counted(true);
  const answer = terminal.prompt("Project number: ");
  input.write("\x04");
  await assert.rejects(answer, { message: "Cancelled" });
  assert.equal(interrupts(), 1);
});

test("Ctrl-D with no question open cancels the command, once", async () => {
  const { input, interrupts } = counted(true);
  input.write("\x04");
  await tick();
  input.write("\x04");
  await tick();
  assert.equal(interrupts(), 1);
});

test("Ctrl-C while a question is open rejects with Cancelled and cancels the command, once", async () => {
  const { input, terminal, interrupts } = counted(true);
  const answer = terminal.prompt("Project number: ");
  input.write("\x03");
  await assert.rejects(answer, { message: "Cancelled" });
  assert.equal(interrupts(), 1);
});

test("Ctrl-C with no question open cancels the command, once", async () => {
  const { input, interrupts } = counted(true);
  input.write("\x03");
  await tick();
  input.write("\x03");
  await tick();
  assert.equal(interrupts(), 1);
});

test("Ctrl-C still cancels after Ctrl-Z and fg", async () => {
  const { input, interrupts } = counted(true);
  // readline stops the process with SIGTSTP on Ctrl-Z; keep this one running.
  const kill = process.kill;
  const signals: unknown[] = [];
  process.kill = ((_pid: number, signal?: string | number) => {
    signals.push(signal);
    return true;
  }) as typeof process.kill;
  try {
    input.write("\x1a");
    await tick();
    assert.deepEqual(signals, ["SIGTSTP"]);
    process.emit("SIGCONT");
    await tick();
    input.write("\x03");
    await tick();
    assert.equal(interrupts(), 1);
  } finally {
    process.kill = kill;
  }
});

test("raw mode is already off when onInterrupt runs", async () => {
  const { input, output } = streams(true);
  const modes: boolean[] = [];
  Object.assign(input, {
    isTTY: true,
    setRawMode: (mode: boolean) => {
      modes.push(mode);
      return input;
    },
  });
  let atInterrupt: boolean[] | undefined;
  terminalPrompt(input, output, () => (atInterrupt ??= [...modes]));
  input.write("\x03");
  await tick();
  assert.deepEqual(modes, [true, false]);
  assert.deepEqual(atInterrupt, [true, false]);
});

test("close() never cancels the command, with or without a question open", async () => {
  const idle = counted(true);
  idle.terminal.close();
  const open = counted(true);
  const answer = open.terminal.prompt("Project number: ");
  open.terminal.close();
  await assert.rejects(answer, { message: "Cancelled" });
  await tick();
  assert.equal(idle.interrupts(), 0);
  assert.equal(open.interrupts(), 0);
});

test("a cancelled question ends its line so the caller's message starts on its own", async () => {
  const { input, terminal, written } = counted();
  const answer = terminal.prompt("Project number: ");
  input.end();
  await assert.rejects(answer, { message: "Cancelled" });
  await tick();
  assert.equal(written(), "Project number: \n");
});

test("a terminal resize with no question open prints no stray prompt", async () => {
  const { output, written } = counted(true);
  await tick();
  output.emit("resize");
  await tick();
  // readline still repositions the cursor on resize; no text may appear.
  assert.equal(stripVTControlCharacters(written()).trim(), "");
});

test("close() with nothing pending writes nothing and leaves no unhandled rejection", async () => {
  const unhandled: unknown[] = [];
  const listener = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", listener);
  try {
    const { terminal, written } = counted();
    terminal.close();
    terminal.close();
    await tick();
    await tick();
    assert.equal(written(), "");
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", listener);
  }
});

test("prompt after close() rejects with Cancelled", async () => {
  const { terminal } = counted();
  terminal.close();
  await assert.rejects(terminal.prompt("Project number: "), {
    message: "Cancelled",
  });
});
