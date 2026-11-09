import { FormField } from '@nous/design-system';

export const Default = () => (
  <div className="w-[360px]">
    <FormField
      label="Project name"
      placeholder="e.g. Sparse attention survey"
      hint="Shown in the sidebar and on shared citation links."
      required
    />
  </div>
);

export const WithError = () => (
  <div className="w-[360px]">
    <FormField
      label="DOI"
      defaultValue="10.48550/arXiv.1706"
      error="Enter a complete DOI, e.g. 10.48550/arXiv.1706.03762"
      required
    />
  </div>
);

export const Filled = () => (
  <div className="grid w-[360px] gap-4">
    <FormField label="Paper title" defaultValue="Attention Is All You Need" />
    <FormField label="Venue" defaultValue="NeurIPS 2017" hint="Optional" />
  </div>
);
