// PopoverTrigger only renders inside its parent - the parent's composition is the true render.
import { ShareThread as ParentShareThread } from './Popover';

export const ShareThread = () => <ParentShareThread />;
