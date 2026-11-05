// TableFooter only renders inside Table - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
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
