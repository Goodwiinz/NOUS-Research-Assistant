// SelectScrollUpButton only appears when an open SelectContent overflows - a long, height-capped
// venue list with a mid-list selection shows the scroll buttons.
import {
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@nous/design-system';

const venues = [
  'AAAI', 'ACL', 'CVPR', 'EMNLP', 'ICCV', 'ICLR', 'ICML', 'IJCAI',
  'KDD', 'NAACL', 'NeurIPS', 'SIGIR', 'TACL', 'WWW',
];

export const LongVenueList = () => (
  <div className="grid w-[280px] gap-2">
    <Label htmlFor="venue">Venue</Label>
    <Select defaultOpen defaultValue="ICML">
      <SelectTrigger id="venue">
        <SelectValue placeholder="Filter by venue" />
      </SelectTrigger>
      <SelectContent className="max-h-48">
        {venues.map((v) => (
          <SelectItem key={v} value={v}>
            {v}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  </div>
);
