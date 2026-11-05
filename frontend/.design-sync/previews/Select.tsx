import {
  Label,
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@nous/design-system';

// Rendered open: in single card mode only the first export is shown, so the
// open menu (trigger + grouped items) comes first.
export const CitationStyleOpen = () => (
  <div className="grid w-[280px] gap-2">
    <Label htmlFor="citation-style">Citation style</Label>
    <Select defaultOpen defaultValue="apa">
      <SelectTrigger id="citation-style">
        <SelectValue placeholder="Choose a style" />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectLabel>Author–date</SelectLabel>
          <SelectItem value="apa">APA 7th edition</SelectItem>
          <SelectItem value="chicago-ad">Chicago (author–date)</SelectItem>
          <SelectItem value="harvard">Harvard</SelectItem>
        </SelectGroup>
        <SelectSeparator />
        <SelectGroup>
          <SelectLabel>Numeric</SelectLabel>
          <SelectItem value="ieee">IEEE</SelectItem>
          <SelectItem value="vancouver">Vancouver</SelectItem>
          <SelectItem value="acm" disabled>
            ACM (coming soon)
          </SelectItem>
        </SelectGroup>
      </SelectContent>
    </Select>
  </div>
);

export const Closed = () => (
  <div className="grid w-[280px] gap-2">
    <Label htmlFor="sort-docs">Sort documents</Label>
    <Select defaultValue="updated">
      <SelectTrigger id="sort-docs">
        <SelectValue placeholder="Sort by" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="updated">Recently updated</SelectItem>
        <SelectItem value="title">Title (A–Z)</SelectItem>
        <SelectItem value="pages">Page count</SelectItem>
      </SelectContent>
    </Select>
  </div>
);
