// Independent of auth/store imports: requests and legacy state owners capture
// this lifetime before awaiting work. Token refresh keeps the same lifetime.
let accountController = new AbortController();
let revision = 0;
const listeners = new Set<() => void>();

export const getAccountSessionRevision = (): number => revision;
export function onAccountSessionReset(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getAccountSignal(): AbortSignal {
  return accountController.signal;
}

export function captureAccountSession(): () => boolean {
  const signal = getAccountSignal();
  return () => !signal.aborted;
}

export function resetAccountSession(): void {
  const previous = accountController;
  accountController = new AbortController();
  revision += 1;
  previous.abort();
  for (const listener of listeners) listener();
}
