import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
} from '@nous/design-system';

export const RenameProject = () => (
  <Dialog defaultOpen modal={false}>
    <DialogContent className="sm:max-w-[440px]">
      <DialogHeader>
        <DialogTitle>Rename project</DialogTitle>
        <DialogDescription>
          The new name shows in the sidebar and on every shared citation link.
        </DialogDescription>
      </DialogHeader>
      <div className="grid gap-2 py-2">
        <Label htmlFor="project-name">Project name</Label>
        <Input id="project-name" defaultValue="Sparse attention survey" />
      </div>
      <DialogFooter>
        <Button variant="outline">Cancel</Button>
        <Button>Save changes</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
);
