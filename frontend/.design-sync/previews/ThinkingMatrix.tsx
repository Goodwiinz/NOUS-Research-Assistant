import { ThinkingMatrix } from '@nous/design-system';

// ThinkingMatrix is the 3x3 sol-gold "working" figure shown before the first
// token arrives. It is decorative (aria-hidden): its only app host is the
// chat's pre-token status pill (`nous-streaming-pill`), which carries the
// live-region label. The pill host below uses that same app class.

export const InStatusPill = () => (
  <div role="status" aria-live="polite" className="nous-streaming-pill">
    <ThinkingMatrix />
    <span>Retrieving sources</span>
  </div>
);

export const PhaseLabels = () => (
  <div className="flex flex-col items-start gap-3">
    {['Thinking', 'Searching arXiv', 'Reading 4 documents', 'Writing answer'].map((label) => (
      <div key={label} role="status" className="nous-streaming-pill">
        <ThinkingMatrix />
        <span>{label}</span>
      </div>
    ))}
  </div>
);
