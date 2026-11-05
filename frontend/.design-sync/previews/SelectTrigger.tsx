// SelectTrigger only renders inside Select - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
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
