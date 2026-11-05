import { Input, Label } from '@nous/design-system';

export const WithLabel = () => (
  <div className="grid w-[360px] gap-2">
    <Label htmlFor="project-name">Project name</Label>
    <Input id="project-name" defaultValue="Sparse attention survey" />
  </div>
);

export const Placeholder = () => (
  <div className="w-[360px]">
    <Input type="search" placeholder="Search papers, notes and citations…" />
  </div>
);

export const Disabled = () => (
  <div className="grid w-[360px] gap-2">
    <Label htmlFor="workspace-id">Workspace ID</Label>
    <Input id="workspace-id" defaultValue="ws_8f2c41e0" disabled />
  </div>
);

export const File = () => (
  <div className="grid w-[360px] gap-2">
    <Label htmlFor="upload">Upload paper</Label>
    <Input id="upload" type="file" accept=".pdf" />
  </div>
);
