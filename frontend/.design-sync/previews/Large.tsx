import { Large, Muted } from '@nous/design-system';

export const EmphasisLine = () => (
  <div className="max-w-[480px] space-y-1">
    <Large>Your review draft is ready</Large>
    <Muted>6 sections · 4,120 words · 31 citations</Muted>
  </div>
);

export const StatCards = () => (
  <div className="flex gap-4">
    {[
      ['38', 'Documents'],
      ['214', 'Citations'],
      ['12', 'Threads'],
    ].map(([value, label]) => (
      <div key={label} className="w-40 rounded-lg border border-border bg-card p-4">
        <Large>{value}</Large>
        <Muted>{label}</Muted>
      </div>
    ))}
  </div>
);
