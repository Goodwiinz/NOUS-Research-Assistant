import { Checkbox, Label } from '@nous/design-system';

export const WithLabel = () => (
  <div className="flex items-center gap-2">
    <Checkbox id="include-preprints" defaultChecked />
    <Label htmlFor="include-preprints">Include arXiv preprints</Label>
  </div>
);

export const States = () => (
  <div className="grid gap-3">
    <div className="flex items-center gap-2">
      <Checkbox id="cb-unchecked" />
      <Label htmlFor="cb-unchecked">Peer-reviewed only</Label>
    </div>
    <div className="flex items-center gap-2">
      <Checkbox id="cb-checked" defaultChecked />
      <Label htmlFor="cb-checked">Cite with page numbers</Label>
    </div>
    <div className="flex items-center gap-2">
      <Checkbox id="cb-disabled" disabled />
      <Label htmlFor="cb-disabled">Share with workspace (owner only)</Label>
    </div>
    <div className="flex items-center gap-2">
      <Checkbox id="cb-disabled-checked" disabled defaultChecked />
      <Label htmlFor="cb-disabled-checked">Store citations in project</Label>
    </div>
  </div>
);

export const SourceFilter = () => (
  <fieldset className="grid gap-3">
    <legend className="mb-2 text-sm font-medium">Search sources</legend>
    {[
      ['src-library', 'Project library', true],
      ['src-arxiv', 'arXiv', true],
      ['src-semantic', 'Semantic Scholar', false],
      ['src-web', 'Web results', false],
    ].map(([id, label, checked]) => (
      <div key={id as string} className="flex items-center gap-2">
        <Checkbox id={id as string} defaultChecked={checked as boolean} />
        <Label htmlFor={id as string} className="font-normal">
          {label as string}
        </Label>
      </div>
    ))}
  </fieldset>
);
