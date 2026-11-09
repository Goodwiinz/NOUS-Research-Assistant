import { CellCitation } from '@nous/design-system';

// The quote-icon evidence button that sits beside an extraction-matrix cell
// value (ExtractionMatrix / CellObservations). Never renders a confidence.
export const InMatrixCell = () => (
  <div className="flex items-center gap-1.5 text-sm" style={{ maxWidth: 360 }}>
    <span>O(n·w) with window w = 512</span>
    <CellCitation
      citation_snippet="Longformer's attention scales linearly with sequence length."
      anchorStatus="verified"
      onOpen={() => {}}
    />
  </div>
);

export const AnchorStatuses = () => (
  <div className="grid gap-2 text-sm" style={{ maxWidth: 360 }}>
    {(['verified', 'ambiguous', 'unverified'] as const).map((s) => (
      <div key={s} className="flex items-center gap-1.5">
        <span className="text-muted-foreground" style={{ width: 96 }}>{s}</span>
        <CellCitation citation_snippet="Sparse attention reduces memory to O(n)." anchorStatus={s} />
      </div>
    ))}
  </div>
);
