export function composeAbortSignals(signals: AbortSignal[]): {
  signal: AbortSignal;
  cleanup: () => void;
} {
  if (signals.length === 1) {
    return { signal: signals[0], cleanup: () => undefined };
  }

  const controller = new AbortController();
  const listeners = signals.map((source) => {
    const relayAbort = (): void => controller.abort(source.reason);
    if (source.aborted) {
      relayAbort();
    } else {
      source.addEventListener('abort', relayAbort, { once: true });
    }
    return { source, relayAbort };
  });

  return {
    signal: controller.signal,
    cleanup: () => {
      listeners.forEach(({ source, relayAbort }) => {
        source.removeEventListener('abort', relayAbort);
      });
    },
  };
}
