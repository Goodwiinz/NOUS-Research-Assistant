// DialogTrigger opens a modal Dialog; DialogContent renders DialogPortal +
// DialogOverlay for you, and DialogClose wraps the dismiss control.
import {
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Input,
  Label,
} from '@nous/design-system';

export const ExportCitations = () => (
  <Dialog defaultOpen>
    <DialogTrigger asChild>
      <Button variant="outline">Export citations</Button>
    </DialogTrigger>
    <DialogContent className="sm:max-w-[440px]">
      <DialogHeader>
        <DialogTitle>Export citations</DialogTitle>
        <DialogDescription>
          Download all 24 references cited across this project as a BibTeX file.
        </DialogDescription>
      </DialogHeader>
      <div className="grid gap-2 py-2">
        <Label htmlFor="export-filename">File name</Label>
        <Input id="export-filename" defaultValue="sparse-attention-survey.bib" className="font-mono text-xs" />
      </div>
      <DialogFooter>
        <DialogClose asChild>
          <Button variant="outline">Cancel</Button>
        </DialogClose>
        <Button>Download .bib</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
);
