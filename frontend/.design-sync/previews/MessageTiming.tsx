import { MessageTiming } from '@nous/design-system';

export const CompletedTurn = () => (
  <MessageTiming
    stats={[
      { label: 'first word', value: '0.8s' },
      { label: 'total', value: '6.4s' },
      { label: 'tokens', value: '3.2k in · 412 out' },
    ]}
    className="w-auto max-w-none gap-x-3 [&>span>span:first-child]:text-(--nous-fg-3) [&>span>span:last-child]:text-(--nous-fg-2)"
  />
);

export const Streaming = () => (
  <MessageTiming
    stats={[{ label: 'total', value: '3.7s' }]}
    streaming
    aria-live="off"
    className="w-auto max-w-none [&>span>span:first-child]:text-(--nous-fg-3)"
  />
);

export const Default = () => (
  <MessageTiming
    stats={[
      { label: 'first word', value: '1.2s' },
      { label: 'total', value: '11.9s' },
    ]}
  />
);
