import { render } from "ink";
import { loadConfig, saveConfig } from "../../frontend/cli/auth/store";
import { loadTranscript, loadBranches } from "./services";
import { reconcileHistory } from "./session";
import { App } from "./app";
import { terminalText } from "./adapter";

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--help")) {
    console.log(
      "Usage: ./nous [--new]\nLogin first with ./nous login. --new starts a fresh thread.",
    );
    return;
  }
  if (args.some((arg) => arg !== "--new"))
    throw new Error("Usage: ./nous [--new]");
  if (!process.stdin.isTTY || !process.stdout.isTTY)
    throw new Error(
      "Ink requires an interactive terminal. Use ./nous for one-shot commands.",
    );
  const config = loadConfig();
  if (!config?.token || !(Date.parse(config.expires_at) > Date.now())) {
    throw new Error("Login required. Run ./nous login first.");
  }
  if (args.includes("--new")) {
    config.thread_id = null;
    saveConfig(config);
  }
  const initialMessages = config.thread_id
    ? await loadTranscript(config.thread_id)
    : [];
  const saved = config.thread_id
    ? await loadBranches(config.thread_id)
    : undefined;
  const initialHistory = reconcileHistory(
    initialMessages,
    saved,
    config.thread_id,
  );
  const instance = render(
    <App initialMessages={initialMessages} initialHistory={initialHistory} />,
    { exitOnCtrlC: false },
  );
  const result = await instance.waitUntilExit();
  if (typeof result === "string") console.log(terminalText(result));
}

main().catch((error: unknown) => {
  console.error(
    terminalText(error instanceof Error ? error.message : String(error)),
  );
  process.exitCode = 1;
});
