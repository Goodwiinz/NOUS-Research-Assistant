import {
  Badge,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';

const documents = [
  { title: 'Attention Is All You Need', type: 'PDF', pages: 15, status: 'Indexed', variant: 'success', updated: 'Oct 14, 2026' },
  { title: 'Longformer: The Long-Document Transformer', type: 'PDF', pages: 17, status: 'Indexed', variant: 'success', updated: 'Oct 13, 2026' },
  { title: 'Big Bird: Transformers for Longer Sequences', type: 'arXiv', pages: 42, status: 'Processing', variant: 'warning', updated: 'Oct 15, 2026' },
  { title: 'Reading notes: sparse attention', type: 'Note', pages: 3, status: 'Queued', variant: 'info', updated: 'Oct 15, 2026' },
  { title: 'Reformer: The Efficient Transformer', type: 'PDF', pages: 12, status: 'Failed', variant: 'destructive', updated: 'Oct 12, 2026' },
] as const;

export const Documents = () => (
  <Table>
    <TableCaption>Documents in “Sparse attention survey”</TableCaption>
    <TableHeader>
      <TableRow>
        <TableHead>Title</TableHead>
        <TableHead>Type</TableHead>
        <TableHead className="text-right">Pages</TableHead>
        <TableHead>Status</TableHead>
        <TableHead>Updated</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      {documents.map((d) => (
        <TableRow key={d.title}>
          <TableCell className="font-medium">{d.title}</TableCell>
          <TableCell className="text-muted-foreground">{d.type}</TableCell>
          <TableCell className="text-right font-mono text-xs">{d.pages}</TableCell>
          <TableCell>
            <Badge variant={d.variant} className="text-[10px]">
              {d.status}
            </Badge>
          </TableCell>
          <TableCell className="text-muted-foreground">{d.updated}</TableCell>
        </TableRow>
      ))}
    </TableBody>
    <TableFooter>
      <TableRow>
        <TableCell colSpan={2}>5 documents</TableCell>
        <TableCell className="text-right font-mono text-xs">89</TableCell>
        <TableCell colSpan={2} />
      </TableRow>
    </TableFooter>
  </Table>
);

export const SelectedRow = () => (
  <Table>
    <TableHeader>
      <TableRow>
        <TableHead>Thread</TableHead>
        <TableHead>Agent</TableHead>
        <TableHead className="text-right">Messages</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableRow>
        <TableCell className="font-medium">Compare efficient attention variants</TableCell>
        <TableCell>Research</TableCell>
        <TableCell className="text-right font-mono text-xs">24</TableCell>
      </TableRow>
      <TableRow data-state="selected">
        <TableCell className="font-medium">Draft related-work section</TableCell>
        <TableCell>Writing</TableCell>
        <TableCell className="text-right font-mono text-xs">11</TableCell>
      </TableRow>
      <TableRow>
        <TableCell className="font-medium">Extract benchmark results</TableCell>
        <TableCell>Data</TableCell>
        <TableCell className="text-right font-mono text-xs">7</TableCell>
      </TableRow>
    </TableBody>
  </Table>
);

// The same document table on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <Documents />
  </DarkSurface>
);
