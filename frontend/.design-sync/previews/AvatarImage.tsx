// AvatarImage only renders inside Avatar - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
import { Avatar, AvatarFallback, AvatarImage } from '@nous/design-system';

export const ImageWithFallback = () => (
  <Avatar>
    <AvatarImage src="/missing-avatar.png" alt="Noam Shazeer" />
    <AvatarFallback className="bg-primary/10 text-primary font-medium">NS</AvatarFallback>
  </Avatar>
);
