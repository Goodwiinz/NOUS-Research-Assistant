import {
  Button,
  Input,
  Label,
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
  Textarea,
} from '@nous/design-system';

export const ProjectSettings = () => (
  <Sheet defaultOpen>
    <SheetTrigger asChild>
      <Button variant="outline">Project settings</Button>
    </SheetTrigger>
    <SheetContent side="right">
      <SheetHeader>
        <SheetTitle>Project settings</SheetTitle>
        <SheetDescription>
          Changes apply to every thread and agent run in this project.
        </SheetDescription>
      </SheetHeader>
      <div className="grid gap-4 py-6">
        <div className="grid gap-2">
          <Label htmlFor="sheet-project-name">Name</Label>
          <Input id="sheet-project-name" defaultValue="Sparse attention survey" />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="sheet-project-brief">Research brief</Label>
          <Textarea
            id="sheet-project-brief"
            rows={4}
            defaultValue="Compare block-sparse, sliding-window and linear attention on long-context benchmarks published since 2020."
          />
        </div>
      </div>
      <SheetFooter>
        <SheetClose asChild>
          <Button variant="outline">Cancel</Button>
        </SheetClose>
        <Button>Save changes</Button>
      </SheetFooter>
    </SheetContent>
  </Sheet>
);
